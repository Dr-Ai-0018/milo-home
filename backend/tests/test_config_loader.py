"""Real TOML/environment resolution, permissions and cwd-independent defaults."""

import os
from pathlib import Path

import pytest

from milo_home.config import Settings
from milo_home.config_loader import (
    ConfigurationError,
    configuration_scope,
    load_configuration,
)


def write_config(tmp_path, text):
    path = tmp_path / "site.toml"
    path.write_text(text)
    path.chmod(0o640)
    return path


def test_toml_settings_and_existing_environment_share_one_resolver(tmp_path):
    path = write_config(
        tmp_path,
        f'[app]\ndatabase = "{tmp_path}/file.sqlite3"\n'
        'origins = ["http://127.0.0.1:7484"]\nsecure_cookie = false\n'
        "auth_window_seconds = 120\n"
        f'[public_assets]\nroot = "{tmp_path}/public"\nmax_bytes = 4096\n',
    )
    configuration = load_configuration(
        path,
        environment={"MILO_DATABASE": str(tmp_path / "env.sqlite3")},
        overrides={"app.database": tmp_path / "cli.sqlite3"},
    )
    settings = Settings.from_configuration(configuration)
    assert settings.database == tmp_path / "cli.sqlite3"
    assert settings.origins == ("http://127.0.0.1:7484",)
    assert not settings.secure_cookie and settings.auth_window_seconds == 120
    assert settings.public_assets.root == tmp_path / "public"
    assert settings.public_assets.max_bytes == 4096
    sources = configuration.source_report()
    assert sources["app.database"] == "cli"
    assert sources["app.origins"] == "toml"
    assert sources["app.body_limit"] == "default"
    assert not any(str(tmp_path) in value for value in sources.values())


def test_environment_only_compatibility_and_partial_pair_override(tmp_path):
    config = load_configuration(
        environment={
            "MILO_DATABASE": str(tmp_path / "home.sqlite3"),
            "MILO_ORIGINS": "https://one.test, https://two.test",
            "MILO_COOKIE_SECURE": "1",
            "MILO_WORK_ATTACHMENT_ROOT": str(tmp_path / "private"),
            "MILO_WORK_ATTACHMENT_MAX_BYTES": "1024",
        }
    )
    settings = Settings.from_configuration(config)
    assert settings.origins == ("https://one.test", "https://two.test")
    assert settings.work_attachments.max_bytes == 1024
    path = write_config(tmp_path, f'[public_assets]\nroot="{tmp_path}/public"\nmax_bytes=1024\n')
    config = load_configuration(path, environment={"MILO_PUBLIC_ASSET_MAX_BYTES": "2048"})
    assert Settings.from_configuration(config).public_assets.max_bytes == 2048


@pytest.mark.parametrize(
    "text",
    [
        '[unknown]\nprivate_value="must-not-leak"',
        '[app]\nprivate_field="must-not-leak"',
        '[app]\ndatabase="relative"',
        '[app]\nsecure_cookie="false"',
        "[app]\nbody_limit=true",
        "[app]\nbody_limit=0",
        "[app]\norigins=[]",
        '[app]\norigins=["https://example.test", ""]',
        '[app]\ndatabase=""',
        "[app]\nbody_limit=2.5",
        "[app]\nbody_limit=",
    ],
)
def test_bad_explicit_file_never_falls_back_or_echoes_values(tmp_path, text):
    path = write_config(tmp_path, text)
    with pytest.raises(ConfigurationError) as caught:
        load_configuration(path, environment={"MILO_DATABASE": str(tmp_path / "valid.sqlite3")})
    assert "must-not-leak" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize(
    "name,value",
    [
        ("MILO_COOKIE_SECURE", "true"),
        ("MILO_DATABASE", ""),
        ("MILO_DATABASE", "relative"),
        ("MILO_ORIGINS", ""),
        ("MILO_BODY_LIMIT", "-1"),
        ("MILO_BODY_LIMIT", "１２"),
        ("MILO_BODY_LIMIT", ""),
        ("MILO_MAINTENANCE_OWNER", "not-a-uuid"),
        ("MILO_BACKUP_UNIT", "--all"),
    ],
)
def test_bad_environment_is_not_replaced_by_file_or_defaults(tmp_path, name, value):
    path = write_config(tmp_path, f'[app]\ndatabase="{tmp_path}/home.sqlite3"\n')
    with pytest.raises(ConfigurationError):
        load_configuration(path, environment={name: value})


def test_absent_explicit_file_and_untrusted_file_fail_closed(tmp_path):
    path = tmp_path / "absent.toml"
    with pytest.raises(ConfigurationError):
        load_configuration(path, environment={})
    path = write_config(tmp_path, "[app]\n")
    path.chmod(0o666)
    with pytest.raises(ConfigurationError):
        load_configuration(path, environment={})
    path.chmod(0o640)
    path.write_bytes(b"#" * (128 * 1024 + 1))
    with pytest.raises(ConfigurationError):
        load_configuration(path, environment={})


def test_defaults_do_not_depend_on_cwd_or_create_files(tmp_path, monkeypatch):
    environment = {"XDG_STATE_HOME": str(tmp_path / "state")}
    first = load_configuration(environment=environment).get("app.database")
    monkeypatch.chdir(tmp_path)
    assert load_configuration(environment=environment).get("app.database") == first
    assert first == tmp_path / "state" / "milo-home" / "home.sqlite3"
    assert not first.parent.exists()
    home_default = load_configuration(environment={}).get("app.database")
    assert home_default == Path.home() / ".local/state/milo-home/home.sqlite3"
    with pytest.raises(ValueError):
        load_configuration(environment={"XDG_STATE_HOME": ""}).get("app.database")


def test_scope_freezes_relative_file_selection_and_resets_without_env_mutation(
    tmp_path, monkeypatch
):
    path = write_config(tmp_path, f'[app]\ndatabase="{tmp_path}/file.sqlite3"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MILO_CONFIG", raising=False)
    with configuration_scope(Path("site.toml")):
        monkeypatch.chdir(tmp_path.parent)
        assert Settings.from_env().database == tmp_path / "file.sqlite3"
        assert load_configuration().path == path
    assert load_configuration(environment={}).path is None


def test_incomplete_optional_section_is_not_implicitly_enabled(tmp_path):
    path = write_config(tmp_path, f'[work_attachments]\nroot="{tmp_path}/files"\n')
    with pytest.raises(ConfigurationError, match="Incomplete work_attachments"):
        Settings.from_configuration(load_configuration(path, environment={}))


def test_root_owned_application_config_can_be_read_by_a_service_identity(tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("Root-owned fixture metadata requires root")
    path = write_config(tmp_path, "[app]\nbody_limit=1024\n")
    # Exercise the ownership policy separately from OS DAC/access verification.
    monkeypatch.setattr("milo_home.config_loader.os.geteuid", lambda: 65534)
    assert load_configuration(path, environment={}).get("app.body_limit") == 1024


def test_committed_development_example_is_loadable_without_runtime_or_credentials():
    root = Path(__file__).parents[2]
    configuration = load_configuration(root / "examples/deployment.toml", environment={})
    settings = Settings.from_configuration(configuration)
    assert settings.origins == ("http://127.0.0.1:7473",)
    assert not settings.secure_cookie
    assert settings.work_attachments is None and settings.maintenance is None

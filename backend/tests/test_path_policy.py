"""One declared path policy across all private operations, with no live data."""

import json
import os
from pathlib import Path

import pytest
from test_codex_config import PROFILE

from milo_home.backup_rehearsal import rehearse_backup
from milo_home.cli_commands import private_output
from milo_home.codex_app_server import ThreadBinding
from milo_home.codex_config import CodexRuntime, NewawzConfig
from milo_home.codex_journal import PrivateJournal
from milo_home.config import PublicAssetSettings, Settings, WorkAttachmentSettings
from milo_home.config_loader import configuration_scope
from milo_home.database import Database
from milo_home.database_identity import private_run_directory
from milo_home.path_policy import PathPolicy
from milo_home.run_environment import prepare
from milo_home.sqlite_readonly import private_database_file


@pytest.fixture
def declared(tmp_path):
    home = tmp_path / "personal"
    static = tmp_path / "static"
    home.mkdir(mode=0o700)
    static.mkdir(mode=0o700)
    config = tmp_path / "deployment.toml"
    config.write_text(f'[paths]\nprotected_roots=["{home}"]\nstatic_root="{static}"\n')
    config.chmod(0o640)
    return config, home, static


@pytest.mark.parametrize(
    "operation",
    [
        lambda p: Database(p),
        lambda p: private_database_file(p),
        lambda p: private_run_directory(p),
        lambda p: private_output(p),
        lambda p: prepare(p, {}, None),
        lambda p: rehearse_backup(p.parent / "unopened.sqlite3", p),
    ],
    ids=["database", "inspection", "run-directory", "private-output", "legacy-run", "rehearsal"],
)
def test_every_private_boundary_rejects_declared_roots_before_io(declared, operation):
    config, home, static = declared
    with configuration_scope(config):
        for root in (home, static):
            target = root / "must-not-create"
            with pytest.raises(ValueError, match="outside"):
                operation(target)
            assert not target.exists()


def test_settings_share_real_static_root_and_protect_independent_asset_stores(declared):
    config, home, static = declared
    with configuration_scope(config):
        paths = PathPolicy.configured()
    normal = home.parent / "private.sqlite3"
    for target in (home / "db.sqlite3", static / "db.sqlite3", paths.package_root / "db.sqlite3"):
        with pytest.raises(ValueError, match="outside"):
            Settings(database=target, origins=("https://home.test",), paths=paths)
    for cls, key in (
        (PublicAssetSettings, "public_assets"),
        (WorkAttachmentSettings, "work_attachments"),
    ):
        with pytest.raises(ValueError, match="outside"):
            Settings(
                database=normal,
                origins=("https://home.test",),
                paths=paths,
                **{key: cls(static / "assets", 1024)},
            )
    valid = Settings(
        database=normal,
        origins=("https://home.test",),
        paths=paths,
        public_assets=PublicAssetSettings(home.parent / "sealed", 1024),
    )
    assert valid.paths.static_root == static


def test_aliases_and_ancestor_directories_cannot_bypass_declared_roots(declared):
    config, home, static = declared
    alias = home.parent / "alias"
    alias.symlink_to(home, target_is_directory=True)
    with configuration_scope(config):
        policy = PathPolicy.configured()
    with pytest.raises(ValueError, match="outside"):
        policy.require_external(alias / "secret.sqlite3")
    with pytest.raises(ValueError, match="outside"):
        policy.require_external(home.parent, directory=True)
    with pytest.raises(ValueError, match="canonical"):
        PathPolicy(protected_roots=(alias,))
    with pytest.raises(ValueError, match="installed package"):
        PathPolicy(code_root=static)


def test_offline_path_policy_ignores_unrelated_bad_business_environment(declared, monkeypatch):
    config, home, _ = declared
    monkeypatch.setenv("MILO_COOKIE_SECURE", "intentionally-invalid")
    monkeypatch.setenv("MILO_DATABASE", "not-an-absolute-database")
    with configuration_scope(config):
        assert PathPolicy.configured().protected_roots == (home,)
        with pytest.raises(ValueError):
            Settings.from_env()


def test_profile_relocation_preserves_original_journal_bytes_and_identity(tmp_path):
    source = tmp_path / "profile-a.toml"
    relocated = tmp_path / "profile-b.toml"
    for path in (source, relocated):
        path.write_text(PROFILE)
        path.chmod(0o600)
    config = NewawzConfig.load(source)
    home = tmp_path / "runtime"
    home.mkdir(mode=0o700)
    config.materialize(home)
    journal = PrivateJournal(home)
    installation = CodexRuntime(tmp_path / "original-cli", tmp_path / "codex-code-mode-host")
    binding = ThreadBinding("original-thread", config.provider, config.model, tmp_path)
    journal.bind(binding, config, installation)
    with journal._connect() as connection:
        original = connection.execute("SELECT value FROM binding").fetchone()[0]
    fingerprint = journal.fingerprint()
    deployment = tmp_path / "deployment.toml"
    deployment.write_text(f'[execution]\nprofile="{relocated}"\ncli="{tmp_path}/unrelated-cli"\n')
    deployment.chmod(0o640)
    with configuration_scope(deployment):
        loaded = NewawzConfig.load()
        reopened = PrivateJournal(home, initialize=False, read_only=True)
        reopened.bind(binding, loaded, reopened.installation())
    with reopened._connect() as connection:
        assert connection.execute("SELECT value FROM binding").fetchone()[0] == original
    assert reopened.fingerprint() == fingerprint and reopened.installation() == installation
    assert (home / "config.toml").read_bytes() == config.content
    assert json.loads(original)["thread_id"] == "original-thread"


@pytest.mark.skipif(os.geteuid() != 0, reason="Requires an actual service euid transition")
def test_service_identity_can_declare_roots_it_cannot_traverse(tmp_path):
    protected = tmp_path / "private" / "notes"
    protected.mkdir(mode=0o700, parents=True)
    original = os.geteuid()
    try:
        os.seteuid(65534)
        with pytest.raises(PermissionError):
            protected.stat()
        policy = PathPolicy(protected_roots=(protected,))
        # The API's own readable state root is independent of the excluded tree.
        allowed = Path("/var/lib/synthetic-milo-state/home.sqlite3")
        assert policy.require_external(allowed) == allowed
        with pytest.raises(ValueError, match="outside"):
            policy.require_external(protected / "must-not-open.sqlite3")
    finally:
        os.seteuid(original)

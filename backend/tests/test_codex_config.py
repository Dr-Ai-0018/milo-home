import os
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from milo_home.codex_config import (
    CodexRuntime,
    NewawzConfig,
    private_create,
    private_read,
)
from milo_home.codex_process import DirectUnit

PROFILE = """
model = "synthetic-model"
model_provider = "chosen"
model_reasoning_effort = "high"
service_tier = "priority"
sandbox_mode = "read-only"
[features]
code_mode_host = false
[model_providers.chosen]
name = "Synthetic"
base_url = "https://provider.invalid/v1"
env_key = "SYNTHETIC_KEY"
wire_api = "responses"
[model_providers.chosen.http_headers]
"X-Client" = "synthetic"
[model_providers.fallback]
base_url = "https://fallback.invalid"
[mcp_servers.unwanted]
command = "unwanted"
"""


def profile(tmp_path, text=PROFILE):
    path = tmp_path / "newawz.config.toml"
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_only_explicit_profile_is_materialized_with_direct_tools_and_no_retry(tmp_path):
    settings = NewawzConfig.load(profile(tmp_path))
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)
    settings.materialize(home)
    settings.materialize(home)
    value = tomllib.loads(private_read(home / "config.toml").decode())
    assert value["model_provider"] == "chosen"
    assert value["sandbox_mode"] == "danger-full-access"
    assert value["approval_policy"] == "never"
    assert value["features"]["code_mode_host"]
    assert not value["features"]["multi_agent"]
    assert not value["features"]["plugins"]
    assert set(value["model_providers"]) == {"chosen"}
    assert value["model_providers"]["chosen"]["request_max_retries"] == 0
    assert value["model_providers"]["chosen"]["stream_max_retries"] == 0
    assert "mcp_servers" not in value
    assert "content=" not in repr(settings)
    assert len(settings.digest) == 64


def test_reply_model_selection_preserves_provider_and_original_binding(tmp_path):
    path = profile(tmp_path)
    original = path.read_bytes()
    settings = NewawzConfig.load(path)
    selected = settings.with_model("synthetic-next-model")
    before, after = (tomllib.loads(item.content.decode()) for item in (settings, selected))
    assert after.pop("model") == "synthetic-next-model"
    assert before.pop("model") == "synthetic-model"
    assert before == after
    assert selected.provider == settings.provider and selected.env_key == settings.env_key
    assert path.read_bytes() == original and settings.model == "synthetic-model"


@pytest.mark.parametrize("model", ["", "bad\nmodel", "has space", "x" * 201])
def test_invalid_model_selection_is_rejected_without_rewriting_profile(tmp_path, model):
    path = profile(tmp_path)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="model identifier"):
        NewawzConfig.load(path).with_model(model)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "replacement",
    [
        ('model_provider = "chosen"', 'model_provider = "missing"'),
        ('wire_api = "responses"', 'wire_api = "other"'),
        ('env_key = "SYNTHETIC_KEY"', 'env_key = "CODEX_HOME"'),
        ("https://provider.invalid/v1", "https://user:secret@provider.invalid/v1"),
        ("https://provider.invalid/v1", "http://provider.invalid/v1"),
    ],
)
def test_invalid_provider_never_falls_back(tmp_path, replacement):
    with pytest.raises(ValueError, match="Invalid newawz"):
        NewawzConfig.load(profile(tmp_path, PROFILE.replace(*replacement)))


def test_default_missing_public_and_linked_profiles_are_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("MILO_CODEX_PROFILE", raising=False)
    monkeypatch.delenv("MILO_CONFIG", raising=False)
    with pytest.raises(ValueError, match="Explicit"):
        NewawzConfig.load()
    with pytest.raises(FileNotFoundError):
        NewawzConfig.load(tmp_path / "newawz.config.toml")
    path = profile(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        NewawzConfig.load(path)
    path.unlink()
    target = tmp_path / "other"
    target.write_text(PROFILE)
    path.symlink_to(target)
    with pytest.raises(OSError):
        NewawzConfig.load(path)


def test_profile_filename_is_independent_of_its_bytes_and_digest(tmp_path):
    original = profile(tmp_path)
    before = original.read_bytes()
    renamed = tmp_path / "provider.toml"
    renamed.write_bytes(before)
    renamed.chmod(0o600)
    first, second = NewawzConfig.load(original), NewawzConfig.load(renamed)
    assert first.content == second.content and first.digest == second.digest
    assert original.read_bytes() == before and renamed.read_bytes() == before


def test_existing_config_drift_is_preserved_and_rejected(tmp_path):
    settings = NewawzConfig.load(profile(tmp_path))
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)
    private_create(home / "config.toml", b'other = "keep"\n')
    with pytest.raises(ValueError, match="changed"):
        settings.materialize(home)
    assert private_read(home / "config.toml") == b'other = "keep"\n'
    with pytest.raises(FileExistsError):
        private_create(home / "config.toml", b"overwrite")


def test_source_directory_may_be_readable_but_not_writable_by_others(tmp_path):
    source = tmp_path / "source"
    source.mkdir(mode=0o755)
    settings = NewawzConfig.load(profile(source))
    with pytest.raises(ValueError, match="private"):
        settings.materialize(source)
    source.chmod(0o777)
    with pytest.raises(ValueError, match="Invalid newawz"):
        NewawzConfig.load(source / "newawz.config.toml")


@pytest.mark.parametrize("version", ["codex-cli 0.162.1", "codex-cli 0.167.0", "codex-cli 1.0.0"])
def test_cli_and_tool_host_package_must_match_without_a_version_pin(tmp_path, monkeypatch, version):
    cli = tmp_path / "launcher"
    host = tmp_path / "codex-code-mode-host"
    native_cli = tmp_path / "codex"
    for path in [cli, host, native_cli]:
        path.touch(mode=0o700)
    runtime = CodexRuntime(cli, host)
    installed = {str(cli): version, str(native_cli): version}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kw: SimpleNamespace(returncode=0, stdout=installed[args[0]]),
    )
    assert runtime.verify() == version
    assert runtime.command() == [str(cli), "app-server", "--stdio"]
    installed[str(native_cli)] = "codex-cli 2.0.0"
    with pytest.raises(ValueError, match="version"):
        runtime.verify()
    host.unlink()
    with pytest.raises(ValueError, match="executables"):
        runtime.verify()


def test_symlinked_tool_host_must_resolve_to_the_companion_package(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    other = tmp_path / "other"
    selected = tmp_path / "selected"
    for directory in (installed, other, selected):
        directory.mkdir()
    native_cli = installed / "codex"
    native_host = installed / "codex-code-mode-host"
    other_host = other / native_host.name
    for path in (native_cli, native_host, other_host):
        path.touch(mode=0o700)
    cli, host = selected / native_cli.name, selected / native_host.name
    cli.symlink_to(native_cli)
    host.symlink_to(native_host)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kw: SimpleNamespace(returncode=0, stdout="codex-cli 1.0.0"),
    )
    runtime = CodexRuntime(cli, host)
    assert runtime.verify() == "codex-cli 1.0.0"
    host.unlink()
    host.symlink_to(other_host)
    with pytest.raises(ValueError, match="same package"):
        runtime.verify()


def test_workspace_launch_uses_one_exact_untrusted_path(tmp_path, monkeypatch):
    runtime = CodexRuntime(Path("/synthetic/codex"), Path("/synthetic/codex-code-mode-host"))
    monkeypatch.setattr(CodexRuntime, "verify", lambda _: None)
    workspace = tmp_path / '小家 "工作桌"'
    workspace.mkdir()
    _, flag, override, *app = runtime.command(workspace=workspace)
    assert flag == "-c" and app == ["app-server", "--stdio"]
    assert tomllib.loads(override) == {"projects": {str(workspace): {"trust_level": "untrusted"}}}
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical workspace"):
        runtime.command(workspace=alias)


@pytest.mark.skipif(
    os.getuid() != 0 or not Path("/run/systemd/system").exists(), reason="Requires root/systemd"
)
def test_environment_file_quotes_credentials_without_shell_expansion(tmp_path):
    settings = NewawzConfig.load(profile(tmp_path))
    runtime = CodexRuntime(Path("/usr/bin/codex"), tmp_path / "codex-code-mode-host")
    credential = 'synthetic \\ " $HOME `false` %n ; # tail'
    environment = tmp_path / "credential.env"
    private_create(environment, runtime.environment(tmp_path, settings, credential))
    # No secret is printed, even on assertion failure in the child.
    program = (
        "import os,sys; "
        f"sys.exit(0 if os.environ.get('SYNTHETIC_KEY') == {credential!r} "
        f"and os.environ.get('CODEX_HOME') == {str(tmp_path)!r} else 1)"
    )
    unit = DirectUnit(seconds=10)
    process = unit.launch(["/usr/bin/python3", "-c", program], environment_file=environment)
    try:
        assert process.wait(timeout=5) == 0
    finally:
        unit.stop()
        for pipe in [process.stdin, process.stdout, process.stderr]:
            pipe.close()
        environment.unlink()

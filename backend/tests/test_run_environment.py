import json
import os
import tomllib

import pytest

from milo_home.run_environment import codex_arguments, prepare, unit_command


@pytest.mark.skipif(os.getuid() != 0, reason="Isolated runtime preparation requires root")
def test_environment_is_allowlisted_and_credential_stays_outside_root(tmp_path, monkeypatch):
    profile = tmp_path / "source.toml"
    profile.write_text("""model = "test-model"
model_provider = "test_provider"
sandbox_mode = "danger-full-access"
[mcp_servers.unwanted]
command = "must-not-run"
[model_providers.test_provider]
name = "test"
base_url = "https://example.invalid"
env_key = "MILO_SYNTHETIC_KEY"
wire_api = "responses"
""")
    monkeypatch.setenv("MILO_SYNTHETIC_KEY", "synthetic-only")
    base = tmp_path / "run"
    root = prepare(
        base,
        {
            "selected": [],
            "history": [],
        },
        profile,
    )
    config = tomllib.loads((root / "runtime/codex/newawz.config.toml").read_text())
    assert config["sandbox_mode"] == "read-only"
    assert config["features"]["shell_tool"] is False
    assert config["features"]["browser_use"] is False
    assert config["features"]["multi_agent"] is False
    assert config["features"]["multi_agent_v2"] is False
    assert config["agents"]["enabled"] is False
    assert "mcp_servers" not in config
    assert "synthetic-only" not in json.dumps(config)
    assert not (root / "credential.env").exists()
    assert (root / "runtime").stat().st_uid == 65534
    assert (base / "credential.env").stat().st_mode & 0o077 == 0
    command = unit_command("milo-test", root, 3600, codex_arguments())
    assert "CPUQuota=50%" in command and "RuntimeMaxSec=3600s" in command
    assert command[command.index("-p", command.index("/usr/bin/codex")) + 1] == "newawz"
    with pytest.raises(ValueError):
        unit_command("milo-test", root, 3601, [])


@pytest.mark.skipif(os.getuid() != 0, reason="Isolated runtime preparation requires root")
def test_explicit_service_credential_and_configuration_never_fall_back(tmp_path, monkeypatch):
    profile = tmp_path / "newawz.config.toml"
    profile.write_text("invalid file must not be read")
    source = (
        b'model="synthetic"\nmodel_provider="test"\n[model_providers.test]\n'
        b'name="test"\nbase_url="https://example.invalid"\n'
        b'env_key="MILO_SYNTHETIC_KEY"\nwire_api="responses"\n'
    )
    monkeypatch.setenv("MILO_SYNTHETIC_KEY", "shell-secret-must-not-be-used")
    root = prepare(
        tmp_path / "reply",
        {"selected": [], "history": []},
        profile,
        credential="explicit-file-secret",
        configuration=source,
    )
    environment = (root.parent / "credential.env").read_text()
    assert "explicit-file-secret" in environment
    assert "shell-secret-must-not-be-used" not in environment
    config = tomllib.loads((root / "runtime/codex/newawz.config.toml").read_text())
    assert config["sandbox_mode"] == "read-only"
    assert config["features"]["code_mode_host"] is False

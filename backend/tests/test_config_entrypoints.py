"""Config selection through actual public commands, HTTP factory and monitor child."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient

from milo_home.app import create_app
from milo_home.cli import main
from milo_home.cli_arguments import build_parser
from milo_home.config_cli import apply_command_defaults
from milo_home.config_loader import configuration_scope, load_configuration
from milo_home.maintenance_monitor import build_command


@pytest.fixture
def selected(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("MILO_"):
            monkeypatch.delenv(name)
    path = tmp_path / "site.toml"
    path.write_text(
        f'[app]\ndatabase="{tmp_path}/home.sqlite3"\n'
        'origins=["http://127.0.0.1:7484"]\nsecure_cookie=false\n'
        f'[execution]\nrun_directory="{tmp_path}/private"\n'
        f'workspace="{tmp_path}/workspace"\nreply_directory="{tmp_path}/replies"\n'
        'database_user="synthetic-service"\n'
        f'[maintenance]\nbackup_directory="{tmp_path}/backups"\n'
    )
    path.chmod(0o640)
    return path


def invoke(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["milo-home", *map(str, args)])
    main()


def test_config_check_has_sources_only_and_does_not_create_data(selected, monkeypatch, capsys):
    invoke(monkeypatch, "--config", selected, "config-check")
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["valid"] and not report["automatic_execution"]
    assert report["sources"]["app.database"] == "toml"
    assert str(selected.parent) not in output and "synthetic-service" not in output
    assert not (selected.parent / "home.sqlite3").exists()


def test_cli_init_and_http_factory_use_the_same_selected_database(selected, monkeypatch):
    invoke(monkeypatch, "--config", selected, "init-db")
    path = selected.parent / "home.sqlite3"
    identity = path.stat()
    monkeypatch.setenv("MILO_CONFIG", str(selected))
    app = create_app()
    assert app.state.settings.database == path
    with TestClient(app, base_url="http://127.0.0.1:7484") as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/notes").json() == {"notes": [], "next_before": None}
    assert path.stat().st_ino == identity.st_ino
    assert path.stat().st_dev == identity.st_dev


def test_serve_and_global_explicit_overrides_keep_one_app_settings_snapshot(
    selected, monkeypatch, capsys
):
    captured = []
    other = selected.parent / "explicit.sqlite3"
    monkeypatch.setenv("MILO_DATABASE", str(selected.parent / "environment.sqlite3"))
    monkeypatch.setattr(
        "milo_home.cli_commands.uvicorn.run", lambda app, **kw: captured.append(app)
    )
    invoke(monkeypatch, "--config", selected, "--database", other, "serve")
    assert captured[0].state.settings.database == other
    assert not other.exists()
    invoke(monkeypatch, "--config", selected, "--database", other, "config-check")
    assert json.loads(capsys.readouterr().out)["sources"]["app.database"] == "cli"


def test_configured_runtime_defaults_only_apply_to_relevant_commands(selected):
    parser = build_parser()
    with configuration_scope(selected):
        normal = parser.parse_args(["init-db"])
        apply_command_defaults(normal)
        assert normal.database_user is None and normal.run_directory is None
        work = parser.parse_args(["work", "setup", "show"])
        apply_command_defaults(work)
        assert work.run_directory == selected.parent / "private"
        assert work.database_user == "synthetic-service"
        explicit = parser.parse_args(
            ["--run-directory", "/explicit/private", "work", "setup", "show"]
        )
        apply_command_defaults(explicit)
        assert explicit.run_directory == Path("/explicit/private")


def test_monitor_child_keeps_file_selection_and_command_defaults_across_cwd(selected, monkeypatch):
    configuration = load_configuration(selected)
    command = build_command(configuration)
    parsed = build_parser().parse_args(command[3:])
    assert parsed.config == selected and parsed.run_directory == selected.parent / "private"
    monkeypatch.chdir(selected.parent.parent)
    with configuration_scope(parsed.config):
        apply_command_defaults(parsed)
        child = load_configuration()
        assert child.get("app.database") == selected.parent / "home.sqlite3"
        assert parsed.backup_directory == selected.parent / "backups"
        assert parsed.success_file is None and parsed.daily_unit is None


def test_bad_config_help_and_offline_export_do_not_open_live_data(selected, monkeypatch, capsys):
    selected.write_text('not valid TOML: "private-input-must-not-leak"')
    monkeypatch.setenv("MILO_COOKIE_SECURE", "invalid-private-flag")
    monkeypatch.setattr(sys, "argv", ["milo-home", "--config", str(selected), "--help"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    capsys.readouterr()
    schema = selected.parent / "openapi.json"
    invoke(monkeypatch, "--config", selected, "export-openapi", "--out", schema)
    assert "/api/health" in json.loads(schema.read_text())["paths"]
    with pytest.raises(SystemExit) as result:
        invoke(monkeypatch, "--config", selected, "init-db")
    assert result.value.code == 1
    output = capsys.readouterr()
    assert "private-input-must-not-leak" not in output.err + output.out
    assert "invalid-private-flag" not in output.err + output.out
    assert not (selected.parent / "home.sqlite3").exists()


def test_actual_monitor_child_selection_is_supported_by_cli(selected):
    # Run the real selected child entry as a read-only check, never maintenance/apply.
    command = build_command(load_configuration(selected))
    prefix = command[: command.index("maintenance")]
    result = subprocess.run(
        [*prefix, "config-check"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sources"]["app.database"] == "toml"
    assert not (selected.parent / "home.sqlite3").exists()


def test_retention_preview_does_not_require_or_invent_a_workspace(selected):
    args = build_parser().parse_args(["maintenance", "retention-preview"])
    with configuration_scope(selected):
        apply_command_defaults(args)
    assert not hasattr(args, "workspace")
    assert args.reply_directory == selected.parent / "replies"
    assert args.run_directory == selected.parent / "private"


def test_real_loopback_serve_uses_selected_config_from_another_directory(selected):
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        port = socket_.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "milo_home.cli",
            "--config",
            str(selected),
            "--origin",
            origin,
            "serve",
            "--port",
            str(port),
        ],
        cwd=selected.parent,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urlopen(origin + "/api/health", timeout=1) as response:
                    assert json.load(response)["status"] == "ok"
                break
            except URLError:
                assert process.poll() is None, process.stderr.read().decode()
                assert time.monotonic() < deadline, "Isolated server did not become ready"
                time.sleep(0.05)
        assert (selected.parent / "home.sqlite3").is_file()
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def test_configured_profile_is_selected_without_falling_back_to_host_default(selected, monkeypatch):
    from milo_home.codex_config import NewawzConfig

    profile = selected.parent / "newawz.config.toml"
    selected.write_text(
        selected.read_text().replace("[execution]\n", f'[execution]\nprofile="{profile}"\n')
    )
    captured = []

    def read(path, **kwargs):
        captured.append(path)
        raise ValueError("Synthetic profile is intentionally absent")

    monkeypatch.setattr("milo_home.codex_config.private_read", read)
    with configuration_scope(selected), pytest.raises(ValueError, match="Invalid newawz"):
        NewawzConfig.load()
    assert captured == [profile]


def test_private_execution_never_guesses_the_generic_database(selected):
    selected.write_text('[execution]\nrun_directory="/explicit/private"\n')
    args = build_parser().parse_args(["work", "setup", "show"])
    with configuration_scope(selected), pytest.raises(ValueError, match="explicit database"):
        apply_command_defaults(args)

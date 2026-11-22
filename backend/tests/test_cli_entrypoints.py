import json
import os
import subprocess
import sys

import pytest
from test_database_identity import owned_database as owned_database
from test_dispatch import make_case


def command(database, *arguments, extra_env=None):
    return subprocess.run(
        [sys.executable, "-m", "milo_home.cli", *arguments],
        env={**os.environ, "MILO_DATABASE": str(database), **(extra_env or {})},
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_real_init_invitation_backup_and_schema_commands(tmp_path):
    database = tmp_path / "home.sqlite3"
    tmp_path.chmod(0o700)
    initialized = command(database, "init-db")
    assert initialized.returncode == 0 and database.exists()
    invitation = tmp_path / "invitation.json"
    invited = command(database, "invite", "--handle", "visitor", "--out", str(invitation))
    assert invited.returncode == 0
    original = invitation.read_bytes()
    token = json.loads(original)["token"]
    assert token not in invited.stdout + invited.stderr
    assert invitation.stat().st_mode & 0o777 == 0o600
    repeated = command(database, "invite", "--handle", "visitor", "--out", str(invitation))
    assert repeated.returncode == 1 and invitation.read_bytes() == original
    rejected = tmp_path / "rejected.json"
    duplicate = command(database, "invite", "--handle", "visitor", "--out", str(rejected))
    assert duplicate.returncode == 1 and not rejected.exists()
    backup = tmp_path / "backup.sqlite3"
    assert command(database, "backup", "--out", str(backup)).returncode == 0
    assert command(backup, "doctor").returncode == 0
    unopened = tmp_path / "unopened.sqlite3"
    schema = tmp_path / "openapi.json"
    exported = command(unopened, "export-openapi", "--out", str(schema))
    assert exported.returncode == 0 and not unopened.exists()
    assert "/api/work-tasks/{task_id}/results" in json.loads(schema.read_bytes())["paths"]


@pytest.mark.parametrize(
    "arguments",
    [
        ("--database-user", "nobody", "init-db"),
        (
            "--run-directory",
            "/synthetic/private-runtime",
            "stop",
            "--run",
            "12345678-1234-1234-1234-123456789abc",
        ),
        ("--database-user", "nobody", "--run-directory", "/synthetic/private-runtime", "doctor"),
    ],
)
def test_account_runtime_validation_never_initializes_or_leaks_arguments(tmp_path, arguments):
    database = tmp_path / "untouched.sqlite3"
    result = command(database, *arguments)
    assert result.returncode == 1 and not database.exists()
    assert result.stderr == "Configuration or file check failed.\n"
    assert not result.stdout


def test_help_and_operational_error_boundary_do_not_open_data(tmp_path):
    database = tmp_path / "unopened.sqlite3"
    invalid = {"MILO_COOKIE_SECURE": "synthetic-private-value"}
    help_ = command(database, "--help", extra_env=invalid)
    assert help_.returncode == 0 and "recover-runs" in help_.stdout
    assert not database.exists()
    failed = command(database, "init-db", extra_env=invalid)
    assert failed.returncode == 1 and failed.stderr == "Configuration or file check failed.\n"
    assert "synthetic-private-value" not in failed.stdout + failed.stderr
    assert not database.exists()


def test_real_status_and_stop_keep_unconfirmed_run_occupied(client):
    thread, runs, _, _ = make_case(client)
    run_id = runs.queue(thread, 60)
    before = command(runs.database.path, "run-status", "--run", run_id)
    assert before.returncode == 0 and json.loads(before.stdout)["status"] == "queued"
    stopped = command(runs.database.path, "stop", "--run", run_id)
    assert stopped.returncode == 0
    after = command(runs.database.path, "run-status", "--run", run_id)
    assert after.returncode == 0 and json.loads(after.stdout)["status"] == "stopping"
    assert runs.active()["id"] == run_id


def test_serve_keeps_resolved_settings_loopback_and_no_access_log(monkeypatch, tmp_path):
    from milo_home.cli import main

    received = []
    monkeypatch.setenv("MILO_DATABASE", str(tmp_path / "must-not-open.sqlite3"))
    monkeypatch.setattr(sys, "argv", ["milo-home", "serve", "--port", "7488"])
    monkeypatch.setattr(
        "milo_home.cli_commands.uvicorn.run", lambda *a, **kw: received.append((a, kw))
    )
    main()
    assert len(received) == 1
    (app,), options = received[0]
    assert app.state.settings.database == tmp_path / "must-not-open.sqlite3"
    assert options == {
        "host": "127.0.0.1",
        "port": 7488,
        "proxy_headers": False,
        "access_log": False,
    }
    assert not (tmp_path / "must-not-open.sqlite3").exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="Requires root/service account boundary")
def test_real_empty_recovery_preserves_service_database_and_root_runtime(
    owned_database, private_runtime_directory
):
    database, account = owned_database
    runtime = private_runtime_directory("milo-cli-entry-")
    result = command(
        database,
        "--database-user",
        account.pw_name,
        "--run-directory",
        str(runtime),
        "recover-runs",
    )
    assert result.returncode == 0, result.stderr
    assert all(p.stat().st_uid == account.pw_uid for p in database.parent.iterdir())
    assert (runtime / "worker.lock").stat().st_uid == 0

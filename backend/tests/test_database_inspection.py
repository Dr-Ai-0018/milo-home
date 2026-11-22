import json
import os
import sqlite3
import subprocess
import sys

import pytest
from test_database import old_database
from test_dispatch import make_case

from milo_home.database import CURRENT_SCHEMA_VERSION, Database
from milo_home.database_inspection import inspect_database, readonly_snapshot


def command(database, *arguments):
    return subprocess.run(
        [sys.executable, "-m", "milo_home.cli", *arguments],
        env={**os.environ, "MILO_DATABASE": str(database)},
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_doctor_has_no_initialization_side_effects(tmp_path):
    missing = tmp_path / "missing" / "home.sqlite3"
    response = command(missing, "doctor")
    assert response.returncode == 1
    assert not missing.parent.exists()
    assert response.stdout == ""
    path = old_database(tmp_path)
    original = path.read_bytes()
    response = command(path, "doctor")
    assert response.returncode == 1
    report = json.loads(response.stdout)
    assert report["schema_version"] == 1
    assert report["migration_required"]
    assert "active_work_windows" not in report
    assert "active_main_creations" not in report
    assert "private_runtime_required" not in report
    assert path.read_bytes() == original


def test_inspection_observes_committed_wal_without_consuming_or_recovering(client):
    _, runs, dispatch, now = make_case(client)
    ticket = dispatch.reserve(120)
    runs.stop(ticket.run_id, "interrupted")
    with runs.database.connect() as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE wake_event_cursor SET last_scan_at = ?", (now[0] + 180,))
        assert runs.database.path.with_suffix(".sqlite3-wal").stat().st_size > 0
        before = list(writer.iterdump())
        report = inspect_database(runs.database.path, clock=lambda: now[0] + 121)
        assert list(writer.iterdump()) == before
    assert report["database_ok"]
    assert report["active_runs"][0]["id"] == ticket.run_id
    assert report["active_runs"][0]["status"] == "stopping"
    assert report["active_runs"][0]["deadline_exceeded"]
    assert "active_run_needs_observation" in report["attention"]
    assert "event_clock_ahead" in report["attention"]
    assert runs.active()["id"] == ticket.run_id
    assert ticket.token not in json.dumps(report)
    assert "token_hash" not in json.dumps(report)
    with readonly_snapshot(runs.database.path) as reader:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM wake_dispatches")


def test_blocked_messages_and_cli_output_do_not_leak_private_records(client):
    _, runs, dispatch, _ = make_case(client)
    ticket = dispatch.reserve(60)
    runs.finish_failed(ticket.run_id, "configuration_error")
    dispatch.reconcile()
    response = command(runs.database.path, "doctor")
    assert response.returncode == 0, response.stderr
    report = json.loads(response.stdout)
    assert report["blocked_messages"] == 2
    assert "automatic_messages_blocked" in report["attention"]
    assert report["active_runs"] == []
    assert report["automatic_wakeup"] is False
    with runs.database.connect() as connection:
        for row in connection.execute("SELECT body FROM messages"):
            assert row[0] not in response.stdout
        for row in connection.execute("SELECT password_hash FROM users"):
            assert row[0] not in response.stdout
        token_hash = connection.execute("SELECT token_hash FROM wake_dispatches").fetchone()[0]
    assert ticket.token not in response.stdout + response.stderr
    assert token_hash not in response.stdout + response.stderr


def test_future_schema_and_corruption_are_not_repaired_or_echoed(tmp_path):
    path = tmp_path / "future.sqlite3"
    database = Database(path)
    database.initialize()
    with database.connect(write=True) as connection:
        connection.execute("PRAGMA user_version=99")
    report = inspect_database(path)
    assert not report["database_ok"]
    assert report["attention"] == ["schema_unsupported"]
    original = b"synthetic-private-data-not-a-database"
    path.write_bytes(original)
    response = command(path, "doctor")
    assert response.returncode == 1
    assert original.decode() not in response.stdout + response.stderr
    assert path.read_bytes() == original


def test_foreign_key_failure_is_reported_without_row_values(tmp_path):
    database = Database(tmp_path / "bad-foreign-key.sqlite3")
    database.initialize()
    with sqlite3.connect(database.path) as connection:
        connection.execute("INSERT INTO threads VALUES ('synthetic-thread', 'missing-user', 1)")
    report = inspect_database(database.path)
    assert not report["foreign_keys_ok"]
    assert not report["database_ok"]
    assert report["attention"] == ["foreign_keys_failed"]
    assert "synthetic-thread" not in json.dumps(report)


def test_private_paths_and_unconfigured_schedule(tmp_path):
    path = tmp_path / "home.sqlite3"
    Database(path).initialize()
    report = inspect_database(path)
    assert report["database_ok"]
    assert report["attention"] == ["schedule_unconfigured"]
    assert report["active_work_windows"] == report["active_main_creations"] == []
    assert report["private_runtime_required"] is False
    linked = tmp_path / "link.sqlite3"
    linked.symlink_to(path)
    with pytest.raises(ValueError, match="regular"):
        inspect_database(linked)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        inspect_database(path)
    with pytest.raises(ValueError, match="absolute"):
        inspect_database(type(path)("relative.sqlite3"))


def test_version_alone_does_not_hide_a_missing_required_table(tmp_path):
    path = tmp_path / "missing-table.sqlite3"
    database = Database(path)
    database.initialize()
    with database.connect(write=True) as connection:
        connection.execute("DROP TABLE sessions")
    report = inspect_database(path)
    assert report["integrity_ok"]
    assert report["schema_version"] == CURRENT_SCHEMA_VERSION
    assert not report["database_ok"]
    assert not report["required_tables_ok"]
    assert report["attention"] == ["schema_tables_missing"]


def test_snapshot_failure_does_not_create_missing_file(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError), Database(path).snapshot():
        pass
    assert not path.exists()

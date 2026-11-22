import json
import os
import sqlite3
import subprocess
import sys

import pytest
from test_database import old_database
from test_dispatch import make_case

from milo_home.backup_rehearsal import rehearse_backup
from milo_home.database import CURRENT_SCHEMA_VERSION, Database
from milo_home.dispatch import DispatchStore
from milo_home.runs import RunStore


def command(source, destination):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "milo_home.cli",
            "backup-rehearse",
            "--file",
            str(source),
            "--out",
            str(destination),
        ],
        env={
            **os.environ,
            "MILO_DATABASE": str(source.parent / "must-not-open.sqlite3"),
            "MILO_COOKIE_SECURE": "intentionally-invalid",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_real_cli_migrates_only_copy_and_ignores_runtime_database(tmp_path):
    source = old_database(tmp_path)
    original = source.read_bytes()
    destination = tmp_path / "rehearsal.sqlite3"
    process = command(source, destination)
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["rehearsal_only"]
    assert report["source_migrated"] is False
    assert report["copied_schema_version"] == 1
    assert report["schema_version"] == CURRENT_SCHEMA_VERSION
    assert report["record_counts"]["users"] == 1
    assert source.read_bytes() == original
    assert not (tmp_path / "must-not-open.sqlite3").exists()
    assert destination.stat().st_mode & 0o777 == 0o600
    with Database(destination).connect() as connection:
        assert connection.execute("SELECT handle FROM users").fetchone()[0] == "visitor"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    second = command(source, destination)
    assert second.returncode == 1
    assert destination.exists()


def test_copy_retains_blocked_history_and_active_occupancy(client, tmp_path):
    thread, runs, dispatch, _ = make_case(client)
    ticket = dispatch.reserve(60)
    assert dispatch.authorize_launch(ticket)
    runs.started(ticket.run_id)
    runs.finish_failed(ticket.run_id, "execution_failed")
    dispatch.reconcile()
    manual_id = runs.queue(thread, 120)
    with runs.database.connect() as connection:
        before = list(connection.iterdump())
    destination = tmp_path / "restored-history.sqlite3"
    process = command(runs.database.path, destination)
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["active_runs_retained"] == 1
    assert report["blocked_messages"] == 2
    assert "active_run_needs_observation" in report["attention"]
    restored = Database(destination)
    assert RunStore(restored).active()["id"] == manual_id
    assert DispatchStore(restored, runs.clock).reserve(60) is None
    assert RunStore(restored).snapshot(manual_id) == runs.snapshot(manual_id)
    with runs.database.connect() as connection:
        assert list(connection.iterdump()) == before
        for row in connection.execute("SELECT body FROM messages"):
            assert row[0] not in process.stdout
        token_hash = connection.execute("SELECT token_hash FROM wake_dispatches").fetchone()[0]
    assert ticket.token not in process.stdout + process.stderr
    assert token_hash not in process.stdout + process.stderr
    with restored.connect() as connection:
        assert connection.execute("SELECT count(*) FROM wake_run_events").fetchone()[0] == 2
        delivery = connection.execute("SELECT * FROM wake_event_delivery").fetchone()
        assert delivery["attempts"] == 1
        assert delivery["blocked_reason"] == "execution_failed"


def test_later_source_writes_do_not_change_the_copied_snapshot(tmp_path, monkeypatch):
    source = Database(tmp_path / "live.sqlite3")
    source.initialize()
    backup = Database.backup

    def copy_then_write(database, destination):
        backup(database, destination)
        with database.connect(write=True) as connection:
            connection.execute("INSERT INTO auth_buckets VALUES ('synthetic', 1, 1)")

    monkeypatch.setattr(Database, "backup", copy_then_write)
    destination = tmp_path / "snapshot.sqlite3"
    report = rehearse_backup(source.path, destination)
    assert report["record_counts"]["auth_buckets"] == 0
    with source.connect() as connection:
        assert connection.execute("SELECT count(*) FROM auth_buckets").fetchone()[0] == 1


def test_failed_copy_migration_leaves_original_and_private_evidence(tmp_path):
    source = old_database(tmp_path)
    with sqlite3.connect(source) as connection:
        for identifier in ["a", "b"]:
            connection.execute(
                "INSERT INTO runs (id, thread_id, status, created_at) "
                "VALUES (?, 'thread', 'queued', 1)",
                (identifier,),
            )
    original = source.read_bytes()
    destination = tmp_path / "failed-rehearsal.sqlite3"
    response = command(source, destination)
    assert response.returncode == 1
    assert source.read_bytes() == original
    assert destination.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "deadline_at" not in {
            row[1] for row in connection.execute("PRAGMA table_info(runs)")
        }
    assert "UNIQUE" not in response.stderr


def test_invalid_source_and_destination_never_overwrite_files(tmp_path):
    source = tmp_path / "source.sqlite3"
    Database(source).initialize()
    original = source.read_bytes()
    with pytest.raises(ValueError, match="independent"):
        rehearse_backup(source, source)
    existing = tmp_path / "existing.sqlite3"
    existing.write_bytes(b"synthetic existing data")
    existing.chmod(0o600)
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(existing)
    for destination in [existing, linked]:
        with pytest.raises(FileExistsError):
            rehearse_backup(source, destination)
    assert existing.read_bytes() == b"synthetic existing data"
    source_link = tmp_path / "source-link.sqlite3"
    source_link.symlink_to(source)
    output = tmp_path / "not-created.sqlite3"
    with pytest.raises(ValueError, match="regular"):
        rehearse_backup(source_link, output)
    assert not output.exists()
    assert source.read_bytes() == original
    with Database(source).connect(write=True) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="unsupported"):
        rehearse_backup(source, output)
    assert not output.exists()


def test_foreign_key_error_never_creates_a_usable_rehearsal(tmp_path):
    source = tmp_path / "broken.sqlite3"
    Database(source).initialize()
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO threads VALUES ('thread', 'missing-user', 1)")
    destination = tmp_path / "not-usable.sqlite3"
    with pytest.raises(ValueError, match="foreign key"):
        rehearse_backup(source, destination)
    assert not destination.exists()


def test_destination_restrictions_are_checked_before_copying(tmp_path):
    source = tmp_path / "source.sqlite3"
    Database(source).initialize()
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    destination = public / "not-created.sqlite3"
    with pytest.raises(ValueError, match="private"):
        rehearse_backup(source, destination)
    assert not destination.exists()
    with pytest.raises(ValueError, match="private"):
        rehearse_backup(source, type(source)("relative.sqlite3"))

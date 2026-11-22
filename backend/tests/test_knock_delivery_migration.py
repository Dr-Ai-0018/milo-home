import sqlite3
from importlib.resources import files

import pytest
from test_knock_delivery import case, greeting, send
from test_knocks import knock
from test_management_migration import version_eight
from test_runs import result

from milo_home import database as module
from milo_home.backup_rehearsal import rehearse_backup
from milo_home.database import CURRENT_SCHEMA_VERSION, RECORD_TABLES, Database
from milo_home.database_inspection import inspect_database
from milo_home.runs import RunStore


def version_nine(tmp_path):
    database = version_eight(tmp_path)
    with database.connect() as connection:
        connection.executescript(
            files("milo_home").joinpath("migrations/009_management_lease.sql").read_text()
        )
        connection.execute("PRAGMA user_version=9")
    return database


def test_upgrade_preserves_all_old_rows_and_does_not_invent_delivery(tmp_path):
    database = version_nine(tmp_path)
    with database.connect() as connection:
        before = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in RECORD_TABLES
            if table != "run_knocks"
        }
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert connection.execute("SELECT count(*) FROM run_knocks").fetchone()[0] == 0
        for table, rows in before.items():
            assert [dict(row) for row in connection.execute(f"SELECT * FROM {table}")] == rows
    assert inspect_database(database.path)["database_ok"]


def test_failed_upgrade_rolls_back_table_indexes_triggers_and_version(tmp_path, monkeypatch):
    database = version_nine(tmp_path)
    original = module.files
    broken = tmp_path / "broken.sql"
    broken.write_text(
        original("milo_home").joinpath("migrations/010_run_knocks.sql").read_text()
        + "\nINSERT INTO missing VALUES (1);\n"
    )

    class Resources:
        def joinpath(self, *parts):
            if parts[-1] == "010_run_knocks.sql":
                return broken
            return original("milo_home").joinpath(*parts)

    monkeypatch.setattr(module, "files", lambda package: Resources())
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN ('run_knocks', 'run_knocks_signal', "
            "'knocks_conversation_signals', 'run_knocks_scope_insert', 'run_knocks_immutable')"
        ).fetchall()
        assert connection.execute("SELECT count(*) FROM knocks").fetchone()[0] == 1


def test_backup_keeps_frozen_and_consumed_signals_without_replaying(client, tmp_path):
    thread, runs, _, now = case(client)
    knock(client, thread)
    first = runs.queue(thread, 60)
    runs.started(first)
    assert runs.publish(first, result(runs, first))
    now[0] += 30
    receipt = knock(client, thread).json()
    send(client, thread)
    second = runs.queue(thread, 60)
    destination = tmp_path / "knock-rehearsal.sqlite3"
    report = rehearse_backup(runs.database.path, destination)
    assert report["active_runs_retained"] == 1
    assert report["record_counts"]["run_knocks"] == 2
    restored = RunStore(Database(destination), runs.clock)
    assert restored.snapshot(second) == runs.snapshot(second)
    assert greeting(restored, thread, now)["id"] == receipt["id"]
    restored.started(second)
    assert restored.publish(second, result(restored, second))
    assert greeting(restored, thread, now) is None
    assert runs.get(second)["status"] == "queued"
    assert greeting(runs, thread, now)["id"] == receipt["id"]


@pytest.mark.parametrize("damage", ["missing_trigger", "cross_scope"])
def test_doctor_and_rehearsal_reject_missing_or_broken_knock_constraints(client, tmp_path, damage):
    thread, runs, _, now = case(client)
    knock(client, thread)
    run = runs.queue(thread, 60)
    merged = knock(client, thread).json() if damage == "cross_scope" else None
    with runs.database.connect(write=True) as connection:
        if damage == "missing_trigger":
            connection.execute("DROP TRIGGER run_knocks_scope_insert")
        else:
            connection.execute("DROP TRIGGER run_knocks_immutable")
            connection.execute("UPDATE run_knocks SET knock_id = ?", (merged["id"],))
            connection.execute(
                "CREATE TRIGGER run_knocks_immutable BEFORE UPDATE ON run_knocks "
                "BEGIN SELECT RAISE(ABORT, 'immutable'); END"
            )
    report = inspect_database(runs.database.path)
    assert not report["database_ok"] and not report["knock_constraints_ok"]
    assert report["attention"] == ["knock_constraints_failed"]
    assert runs.get(run)["status"] == "queued"
    if damage == "cross_scope":
        with pytest.raises(ValueError, match="Invalid run knock snapshot"):
            runs.snapshot(run)
    with pytest.raises(ValueError, match="inspection"):
        rehearse_backup(runs.database.path, tmp_path / "invalid-rehearsal.sqlite3")

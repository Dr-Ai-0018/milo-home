import re
import sqlite3
from importlib.resources import files

import pytest
from test_conversation_migration import version_five

from milo_home.database import CURRENT_SCHEMA_VERSION, MIGRATIONS, Database


def remove_reconciliation_schema(connection):
    """Reconstruct a pre-v17 test database, including its original permit guard."""
    remove_main_creation_schema(connection)
    connection.execute("DROP TRIGGER execution_permit_state_transition")
    connection.execute("DROP TABLE work_reconciliations")
    migration = (
        files("milo_home").joinpath("migrations", "013_shared_execution_budget.sql").read_text()
    )
    trigger = re.search(
        r"CREATE TRIGGER execution_permit_state_transition\b.*?END;", migration, re.S
    )
    connection.execute(trigger[0])


def remove_main_creation_schema(connection):
    connection.execute("DROP TABLE IF EXISTS reply_execution_results")
    connection.execute("DROP TABLE IF EXISTS reply_run_scopes")
    connection.execute("DROP TABLE IF EXISTS automatic_services")
    connection.execute("DROP TABLE IF EXISTS work_attachments")
    connection.execute("DROP TABLE IF EXISTS work_attachment_batches")
    connection.execute("DROP TRIGGER IF EXISTS work_receipt_task_created")
    connection.execute("DROP TABLE IF EXISTS work_receipt_tasks")
    connection.execute("DROP TRIGGER IF EXISTS work_steer_no_start")
    connection.execute("DROP TRIGGER IF EXISTS work_steer_permit_end")
    connection.execute("DROP TRIGGER IF EXISTS work_steer_window_end")
    connection.execute("DROP TRIGGER IF EXISTS work_steer_window_unknown")
    connection.execute("DROP TABLE IF EXISTS work_steer_inputs")
    connection.execute("DROP TABLE IF EXISTS work_steer_turns")
    connection.execute("DROP TABLE IF EXISTS work_deliverables")
    connection.execute("DROP TABLE IF EXISTS work_artifacts")
    for suffix in ["legacy_insert", "legacy_update", "work_insert", "work_update"]:
        connection.execute(f"DROP TRIGGER main_creation_blocks_{suffix}")
    connection.execute("DROP TRIGGER IF EXISTS main_creation_successor_authorized")
    connection.execute("DROP TABLE IF EXISTS work_main_attempt_grants")
    connection.execute("DROP TABLE IF EXISTS work_main_attempt_retirements")
    connection.execute("DROP TABLE work_main_creations")


def version_ten(tmp_path):
    database = version_five(tmp_path)
    with sqlite3.connect(database.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for version, name in MIGRATIONS:
            if 5 < version <= 10:
                connection.executescript(
                    files("milo_home").joinpath("migrations", name).read_text()
                )
        connection.execute("PRAGMA user_version=10")
    return database


def snapshot(database):
    with database.connect() as connection:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            name: [dict(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY rowid")]
            for name in names
        }


def test_v10_migration_keeps_every_old_table_and_does_not_grant_access(tmp_path):
    database = version_ten(tmp_path)
    before = snapshot(database)
    database.initialize()
    database.initialize()
    after = snapshot(database)
    for name, rows in before.items():
        assert after[name] == rows
    assert all(not after[name] for name in set(after) - set(before) - {"execution_policy"})
    assert after["execution_policy"][0]["policy"] is None
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    backup = tmp_path / "restored.sqlite3"
    database.backup(backup)
    restored = Database(backup)
    restored.initialize()
    assert snapshot(restored) == after


@pytest.mark.parametrize("starting_version", range(10, CURRENT_SCHEMA_VERSION))
def test_failed_work_migration_rolls_back_all_new_tables_and_version(
    tmp_path, monkeypatch, starting_version
):
    from milo_home import database as module

    database = version_ten(tmp_path)
    if starting_version > 10:
        with sqlite3.connect(database.path) as connection:
            for version, name in MIGRATIONS:
                if 10 < version <= starting_version:
                    connection.executescript(
                        files("milo_home").joinpath("migrations", name).read_text()
                    )
            connection.execute(f"PRAGMA user_version={starting_version}")
    before = snapshot(database)
    original = module.files
    broken = tmp_path / "broken.sql"
    broken.write_text("CREATE TABLE work_partial (id INTEGER);\nINSERT INTO missing VALUES (1);\n")

    class Resources:
        def joinpath(self, *parts):
            return (
                broken
                if parts[-1].startswith(f"{starting_version + 1:03}_")
                else original("milo_home").joinpath(*parts)
            )

    with monkeypatch.context() as context:
        context.setattr(module, "files", lambda package: Resources())
        with pytest.raises(sqlite3.OperationalError):
            database.initialize()
    assert snapshot(database) == before
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == starting_version


def test_v12_history_is_not_reset_or_duplicated_by_common_budget_upgrade(tmp_path):
    from test_schedules import timestamp

    from milo_home.execution_budget import ExecutionBudget, ExecutionPolicy

    database = version_ten(tmp_path)
    now = timestamp("10:00")
    with database.connect(write=True) as connection:
        for version, name in MIGRATIONS:
            if 10 < version <= 12:
                connection.executescript(
                    files("milo_home").joinpath("migrations", name).read_text()
                )
        connection.execute("PRAGMA user_version=12")
        connection.execute("UPDATE runs SET created_at=?,deadline_at=?", (now, now + 60))
        connection.execute("UPDATE runs SET ended_at=? WHERE id='ended'", (now + 3,))
        connection.execute("UPDATE runs SET ended_at=? WHERE id='replied'", (now + 2,))
    before = snapshot(database)
    database.initialize()
    database.initialize()
    budget = ExecutionBudget(database, lambda: now + 3)
    budget.configure(ExecutionPolicy(daily_launches=10, daily_seconds=1000))
    report = budget.view()
    assert (report["held_or_charged_launches"], report["held_or_charged_seconds"]) == (3, 65)
    after = snapshot(database)
    for name, rows in before.items():
        assert after[name] == rows
    assert after["legacy_launches"] == []
    restored_path = tmp_path / "upgraded-backup.sqlite3"
    database.backup(restored_path)
    restored = Database(restored_path)
    restored.initialize()
    assert ExecutionBudget(restored, lambda: now + 3).view() == report

import sqlite3
from importlib.resources import files

import pytest
from test_knock_migration import version_seven

from milo_home import database as module
from milo_home.automatic_budget import AutomaticPolicy
from milo_home.database import CURRENT_SCHEMA_VERSION, RECORD_TABLES, Database
from milo_home.database_inspection import inspect_database
from milo_home.management_lease import ManagementLease


def version_eight(tmp_path):
    database = version_seven(tmp_path)
    with database.connect() as connection:
        connection.executescript(
            files("milo_home").joinpath("migrations/008_knocks.sql").read_text()
        )
        connection.execute("PRAGMA user_version=8")
        connection.execute(
            "INSERT INTO knocks VALUES ('receipt', 'owner', 'thread', 'thread', 'key', "
            "'receipt', 'accepted', 1, 31, 'unknown')"
        )
    return database


def test_v8_upgrade_retains_every_record_and_backup_keeps_lease(tmp_path):
    database = version_eight(tmp_path)
    tables = [table for table in RECORD_TABLES if table not in {"management_lease", "run_knocks"}]
    with database.connect() as connection:
        before = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert connection.execute("SELECT count(*) FROM management_lease").fetchone()[0] == 0
        for table in tables:
            assert [dict(row) for row in connection.execute(f"SELECT * FROM {table}")] == before[
                table
            ]
    lease = ManagementLease(database, AutomaticPolicy(daily_launches=2, daily_seconds=120))
    lease.claim()
    backup = tmp_path / "management-backup.sqlite3"
    database.backup(backup)
    restored = Database(backup)
    restored.initialize()
    assert inspect_database(backup)["database_ok"]
    with restored.connect(write=True) as connection:
        assert connection.execute("SELECT policy_hash FROM management_lease").fetchone()[0]
        connection.execute("DROP TABLE management_lease")
    assert not inspect_database(backup)["database_ok"]


def test_failed_v9_migration_restores_schema_and_old_records(tmp_path, monkeypatch):
    database = version_eight(tmp_path)
    original = module.files
    broken = tmp_path / "broken.sql"
    broken.write_text(
        original("milo_home").joinpath("migrations/009_management_lease.sql").read_text()
        + "\nINSERT INTO missing VALUES (1);\n"
    )

    class Resources:
        def joinpath(self, *parts):
            if parts[-1] == "009_management_lease.sql":
                return broken
            return original("milo_home").joinpath(*parts)

    monkeypatch.setattr(module, "files", lambda package: Resources())
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'management_lease'"
        ).fetchone()
        assert connection.execute("SELECT count(*) FROM knocks").fetchone()[0] == 1

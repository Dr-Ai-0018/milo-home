import sqlite3
from importlib.resources import files

import pytest
from test_activity_migration import version_six

from milo_home import database as module
from milo_home.database import CURRENT_SCHEMA_VERSION, RECORD_TABLES, Database
from milo_home.database_inspection import inspect_database


def version_seven(tmp_path):
    database = version_six(tmp_path)
    with database.connect() as connection:
        connection.executescript(
            files("milo_home").joinpath("migrations/007_run_activity.sql").read_text()
        )
        connection.execute("PRAGMA user_version=7")
        connection.execute(
            "INSERT INTO run_activity VALUES ('replied', 'synthetic-identity-hash', 1, 1, 0)"
        )
    return database


def test_v7_upgrade_preserves_all_existing_records_and_backups(tmp_path):
    database = version_seven(tmp_path)
    tables = [
        table
        for table in RECORD_TABLES
        if table not in {"knocks", "management_lease", "run_knocks"}
    ]
    with database.connect() as connection:
        before = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        for table in tables:
            assert [dict(row) for row in connection.execute(f"SELECT * FROM {table}")] == before[
                table
            ]
        assert connection.execute("SELECT count(*) FROM knocks").fetchone()[0] == 0
        connection.execute(
            "INSERT INTO knocks VALUES ('receipt', 'owner', 'thread', 'thread', 'key', "
            "'receipt', 'accepted', 1, 31, 'unknown')"
        )
    backup = tmp_path / "knock-backup.sqlite3"
    database.backup(backup)
    restored = Database(backup)
    restored.initialize()
    assert inspect_database(backup)["database_ok"]
    with restored.connect(write=True) as connection:
        assert connection.execute("SELECT next_allowed_at FROM knocks").fetchone()[0] == 31
        connection.execute("DROP TABLE knocks")
    assert not inspect_database(backup)["database_ok"]


def test_failed_v8_upgrade_rolls_back_table_index_trigger_and_version(tmp_path, monkeypatch):
    database = version_seven(tmp_path)
    original = module.files
    broken = tmp_path / "broken.sql"
    broken.write_text(
        original("milo_home").joinpath("migrations/008_knocks.sql").read_text()
        + "\nINSERT INTO missing VALUES (1);\n"
    )

    class Resources:
        def joinpath(self, *parts):
            if parts[-1] == "008_knocks.sql":
                return broken
            return original("milo_home").joinpath(*parts)

    monkeypatch.setattr(module, "files", lambda package: Resources())
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE name IN ('knocks', 'conversations_scope_identity', 'knocks_signal_insert')"
            ).fetchall()
            == []
        )
        assert connection.execute("SELECT count(*) FROM run_activity").fetchone()[0] == 1

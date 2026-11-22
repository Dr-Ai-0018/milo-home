import sqlite3
from importlib.resources import files

import pytest
from test_conversation_migration import version_five

from milo_home import database as module
from milo_home.database import CURRENT_SCHEMA_VERSION, Database
from milo_home.database_inspection import inspect_database


def version_six(tmp_path):
    database = version_five(tmp_path)
    with database.connect() as connection:
        connection.executescript(
            files("milo_home").joinpath("migrations/006_conversations.sql").read_text()
        )
        connection.execute("PRAGMA user_version=6")
    return database


def test_v6_upgrade_keeps_active_and_historical_rows_without_fabricating_pulse(tmp_path):
    database = version_six(tmp_path)
    with database.connect() as connection:
        before = [dict(row) for row in connection.execute("SELECT * FROM runs")]
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert [dict(row) for row in connection.execute("SELECT * FROM runs")] == before
        assert connection.execute("SELECT count(*) FROM run_activity").fetchone()[0] == 0
    backup = tmp_path / "restored.sqlite3"
    database.backup(backup)
    Database(backup).initialize()
    assert inspect_database(backup)["database_ok"]
    with Database(backup).connect(write=True) as connection:
        connection.execute("DROP TABLE run_activity")
    assert not inspect_database(backup)["database_ok"]


def test_failed_v7_upgrade_rolls_back_new_table_and_version(tmp_path, monkeypatch):
    database = version_six(tmp_path)
    broken = tmp_path / "broken.sql"
    broken.write_text("CREATE TABLE run_activity (id TEXT);\nINSERT INTO missing VALUES (1);\n")
    original = module.files

    class Resources:
        def joinpath(self, *parts):
            if parts[-1] == "007_run_activity.sql":
                return broken
            return original("milo_home").joinpath(*parts)

    monkeypatch.setattr(module, "files", lambda package: Resources())
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'run_activity'").fetchone()
            is None
        )

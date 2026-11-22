"""Upgrade a real v30 copy without rewriting old rows or creating personal decisions."""

from milo_home import database as module
from milo_home.database import Database


def test_v30_copy_keeps_old_tables_and_empty_decisions(tmp_path, monkeypatch):
    database = Database(tmp_path / "v30.sqlite3")
    with monkeypatch.context() as context:
        context.setattr(
            module, "MIGRATIONS", tuple(item for item in module.MIGRATIONS if item[0] <= 30)
        )
        database.initialize()
    with database.connect(write=True) as connection:
        connection.execute("INSERT INTO users VALUES ('owner','synthetic','合成旧账号','unused',1)")
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
    path = tmp_path / "copy.sqlite3"
    database.backup(path)
    copied = Database(path)
    copied.initialize()
    with copied.snapshot() as connection:
        for table, rows in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == rows
        assert connection.execute("SELECT count(*) FROM work_decisions").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0] == module.CURRENT_SCHEMA_VERSION
        )
    with database.snapshot() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 30

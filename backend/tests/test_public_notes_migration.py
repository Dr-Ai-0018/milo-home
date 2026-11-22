import sqlite3
from importlib.resources import files

import pytest

import milo_home.database as module
from milo_home.database import Database


def test_v33_migration_preserves_every_existing_table_and_adds_no_public_content(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "home.sqlite3")
    current = tuple(m for m in module.MIGRATIONS if m[0] <= 34)
    with monkeypatch.context() as context:
        context.setattr(module, "MIGRATIONS", current[:-1])
        database.initialize()
    with database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO users VALUES (?,?,?,?,?)",
            ("synthetic-user", "visitor", "合成访客", "synthetic-unused-hash", 1),
        )
        names = [
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        before = {
            name: [tuple(row) for row in connection.execute(f'SELECT * FROM "{name}"')]
            for name in names
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 33
    with monkeypatch.context() as context:
        context.setattr(module, "MIGRATIONS", current)
        database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 34
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for name, rows in before.items():
            assert [tuple(row) for row in connection.execute(f'SELECT * FROM "{name}"')] == rows
        assert connection.execute("SELECT count(*) FROM public_notes").fetchone()[0] == 0


def test_failed_migration_rolls_back_schema_and_version(tmp_path, monkeypatch):
    database = Database(tmp_path / "home.sqlite3")
    current = tuple(m for m in module.MIGRATIONS if m[0] <= 34)
    with monkeypatch.context() as context:
        context.setattr(module, "MIGRATIONS", current[:-1])
        database.initialize()
    original = files("milo_home").joinpath("migrations", current[-1][1]).read_text()

    class BrokenMigration:
        def joinpath(self, *parts):
            return self

        def read_text(self):
            return original + "\nSELECT * FROM missing_migration_table;\n"

    monkeypatch.setattr(module, "files", lambda _: BrokenMigration())
    monkeypatch.setattr(module, "MIGRATIONS", current)
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 33
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'public_note%'"
        ).fetchall()

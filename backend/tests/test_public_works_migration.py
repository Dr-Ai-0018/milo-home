"""v34 upgrade preserves all old rows; failed v35 rolls back as one unit."""

import sqlite3
from importlib.resources import files

import pytest
from test_public_notes import content

import milo_home.database as module
from milo_home.database import Database
from milo_home.database_inspection import inspect_database
from milo_home.public_notes import PublicNotes


def old_database(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    database = Database(tmp_path / "home.sqlite3")
    with monkeypatch.context() as patch:
        patch.setattr(module, "MIGRATIONS", tuple(m for m in module.MIGRATIONS if m[0] <= 34))
        database.initialize()
    notes = PublicNotes(database)
    from uuid import uuid4

    draft = notes.create(str(uuid4()), content())
    notes.publish(draft["id"], 1)
    return database


def test_v34_upgrade_preserves_all_rows_and_has_empty_work_catalog(tmp_path, monkeypatch):
    database = old_database(tmp_path, monkeypatch)
    with database.connect() as connection:
        names = [
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        before = {
            name: [tuple(r) for r in connection.execute(f'SELECT * FROM "{name}"')]
            for name in names
        }
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 35
        for name, rows in before.items():
            assert [tuple(r) for r in connection.execute(f'SELECT * FROM "{name}"')] == rows
        assert connection.execute("SELECT count(*) FROM public_works").fetchone()[0] == 0
    assert inspect_database(database.path)["public_works_constraints_ok"]
    with database.connect(write=True) as connection:
        connection.execute("DROP INDEX public_work_asset_references")
    report = inspect_database(database.path)
    assert not report["database_ok"] and report["attention"] == ["public_works_constraints_failed"]


def test_failed_v35_migration_rolls_back_every_new_table(tmp_path, monkeypatch):
    database = old_database(tmp_path, monkeypatch)
    migration = files("milo_home").joinpath("migrations", "035_public_works.sql").read_text()

    class Broken:
        def joinpath(self, *parts):
            return self

        def read_text(self):
            return migration + "\nSELECT * FROM synthetic_missing_table;\n"

    monkeypatch.setattr(module, "files", lambda _: Broken())
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 34
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'public_work%'"
            ).fetchall()
            == []
        )
        assert connection.execute("SELECT count(*) FROM public_notes").fetchone()[0] == 1

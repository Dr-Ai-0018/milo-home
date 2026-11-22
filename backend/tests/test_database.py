import sqlite3
from importlib.resources import files
from uuid import uuid4

import pytest
from conftest import activate, own_thread

from milo_home.database import CURRENT_SCHEMA_VERSION, Database


def test_backup_with_wal_and_reopen(client, tmp_path):
    user, _ = activate(client)
    thread = own_thread(client)
    database = client.app.state.database
    client.app.state.messages.post(user.json()["id"], thread, "备份中的测试留言", str(uuid4()))
    backup = tmp_path / "restored.sqlite3"
    database.backup(backup)
    restored = Database(backup)
    restored.initialize()
    with restored.connect() as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT body FROM messages").fetchone()[0] == "备份中的测试留言"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    with pytest.raises(FileExistsError):
        database.backup(backup)
    with pytest.raises(ValueError):
        database.backup(database.path)


def test_unknown_and_unversioned_databases_are_not_migrated(tmp_path):
    path = tmp_path / "future.sqlite3"
    path.touch(mode=0o600)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="Unsupported"):
        Database(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    path = tmp_path / "unknown.sqlite3"
    path.touch(mode=0o600)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE existing (id INTEGER)")
    with pytest.raises(ValueError, match="Unversioned"):
        Database(path).initialize()


def test_database_rejects_public_permissions_and_symlinks(tmp_path):
    path = tmp_path / "public.sqlite3"
    path.touch(mode=0o644)
    with pytest.raises(ValueError, match="permissions"):
        Database(path).initialize()
    linked = tmp_path / "link.sqlite3"
    linked.symlink_to(path)
    with pytest.raises(ValueError, match="symbolic"):
        Database(linked).initialize()


def old_database(tmp_path):
    path = tmp_path / "v1.sqlite3"
    path.touch(mode=0o600)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            files("milo_home").joinpath("migrations/001_initial.sql").read_text()
        )
        connection.execute("PRAGMA user_version=1")
        connection.execute("INSERT INTO users VALUES ('owner', 'visitor', '访客', 'synthetic', 1)")
        connection.execute("INSERT INTO threads VALUES ('thread', 'owner', 1)")
    return path


def test_v1_upgrade_keeps_records_and_is_repeatable(tmp_path):
    database = Database(old_database(tmp_path))
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("SELECT handle FROM users").fetchone()[0] == "visitor"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert "deadline_at" in {row[1] for row in connection.execute("PRAGMA table_info(runs)")}


def test_failed_upgrade_rolls_back_all_alterations(tmp_path):
    path = old_database(tmp_path)
    with sqlite3.connect(path) as connection:
        for id_ in ["a", "b"]:
            connection.execute(
                "INSERT INTO runs (id, thread_id, status, created_at) "
                "VALUES (?, 'thread', 'queued', 1)",
                (id_,),
            )
    with pytest.raises(sqlite3.IntegrityError):
        Database(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "deadline_at" not in {
            row[1] for row in connection.execute("PRAGMA table_info(runs)")
        }

import sqlite3
from importlib.resources import files

import pytest

from milo_home.database import CURRENT_SCHEMA_VERSION, MIGRATIONS, Database


def version_five(tmp_path):
    path = tmp_path / "v5.sqlite3"
    path.touch(mode=0o600)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for version, name in MIGRATIONS:
            if version > 5:
                break
            connection.executescript(files("milo_home").joinpath("migrations", name).read_text())
        connection.execute("PRAGMA user_version=5")
        connection.execute("INSERT INTO users VALUES ('owner', 'visitor', '合成', 'unused', 1)")
        connection.execute("INSERT INTO threads VALUES ('thread', 'owner', 1)")
        connection.execute(
            "INSERT INTO runs (id, thread_id, status, created_at, deadline_at) "
            "VALUES ('ended', 'thread', 'failed', 1, 100), ('active', 'thread', 'queued', 2, 100), "
            "('replied', 'thread', 'succeeded', 1, 100)"
        )
        connection.execute(
            "INSERT INTO messages (id, thread_id, sequence, role, body, author_id, client_key, "
            "created_at) VALUES "
            "('original', 'thread', 1, 'visitor', '旧信', 'owner', 'old-key', 1), "
            "('message', 'thread', 3, 'visitor', '合成正文', 'owner', 'key', 2)"
        )
        connection.execute(
            "INSERT INTO messages "
            "(id, thread_id, sequence, role, body, in_reply_to, run_id, created_at) "
            "VALUES ('reply', 'thread', 2, 'milo', '旧回信', 'original', 'replied', 1)"
        )
        connection.execute("INSERT INTO run_messages VALUES ('replied', 'original', 'thread')")
        connection.execute("INSERT INTO run_messages VALUES ('active', 'message', 'thread')")
        connection.execute(
            "INSERT INTO wake_events (id, event_key, kind, thread_id, message_id, observed_at) "
            "VALUES ('event', 'message:message', 'message', 'thread', 'message', 1)"
        )
        connection.execute(
            "INSERT INTO wake_dispatches (run_id, token_hash, lease_until) "
            "VALUES ('active', 'synthetic-hash', 50)"
        )
        connection.execute("INSERT INTO wake_event_delivery VALUES ('event', 2, 'active', 0, NULL)")
        connection.execute("INSERT INTO wake_run_events VALUES ('active', 'event')")
    return Database(path)


def test_v5_migration_preserves_rows_attempts_and_active_run(tmp_path):
    database = version_five(tmp_path)
    tables = [
        "users",
        "threads",
        "messages",
        "runs",
        "run_messages",
        "wake_events",
        "wake_dispatches",
        "wake_event_delivery",
        "wake_run_events",
    ]
    with database.connect() as connection:
        before = {
            name: [dict(row) for row in connection.execute(f"SELECT * FROM {name}")]
            for name in tables
        }
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        for name in tables:
            after = [dict(row) for row in connection.execute(f"SELECT * FROM {name}")]
            if name in {"messages", "runs"}:
                assert all(row.pop("conversation_id") == "thread" for row in after)
            assert after == before[name]
        conversation = connection.execute("SELECT * FROM conversations").fetchone()
        assert conversation["id"] == "thread" and conversation["is_default"] == 1
        assert conversation["title"] is None and conversation["created_at"] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    destination = tmp_path / "restored.sqlite3"
    database.backup(destination)
    restored = Database(destination)
    restored.initialize()
    with restored.connect() as connection:
        assert connection.execute("SELECT attempts FROM wake_event_delivery").fetchone()[0] == 2
        assert (
            connection.execute("SELECT status FROM runs WHERE id = 'active'").fetchone()[0]
            == "queued"
        )


def test_v6_failure_rolls_back_new_table_columns_and_version(tmp_path):
    database = version_five(tmp_path)
    with database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_migration BEFORE UPDATE ON messages "
            "BEGIN SELECT RAISE(ABORT, 'synthetic migration failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic migration failure"):
        database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert "conversation_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(messages)")
        }
        assert "conversation_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(runs)")
        }
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'conversations'"
            ).fetchone()
            is None
        )
        assert connection.execute("SELECT attempts FROM wake_event_delivery").fetchone()[0] == 2

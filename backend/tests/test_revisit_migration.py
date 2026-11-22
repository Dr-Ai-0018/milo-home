"""v28 decisions, arrangements and all original table rows remain intact after v29 migration."""

from uuid import uuid4

from milo_home import database as module
from milo_home.database import Database


def test_v28_backup_migrates_without_rewriting_old_business_rows(tmp_path, monkeypatch):
    database = Database(tmp_path / "v28.sqlite3")
    migrations = module.MIGRATIONS
    with monkeypatch.context() as context:
        context.setattr(module, "MIGRATIONS", tuple(item for item in migrations if item[0] <= 28))
        database.initialize()
    owner, thread, message, run, revisit = [str(uuid4()) for _ in range(5)]
    with database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO users VALUES (?,'synthetic','合成旧账号','unused',1)", (owner,)
        )
        connection.execute("INSERT INTO threads VALUES (?,?,1)", (thread, owner))
        conversation = connection.execute(
            "SELECT id FROM conversations WHERE thread_id=?", (thread,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO messages "
            "(id,thread_id,sequence,author_id,role,body,client_key,created_at) "
            "VALUES (?,?,1,?,'visitor','合成旧暂缓',?,1)",
            (message, thread, owner, str(uuid4())),
        )
        connection.execute(
            "INSERT INTO runs "
            "(id,thread_id,conversation_id,status,created_at,ended_at,deadline_at,unit_name) "
            "VALUES (?,?,?,'succeeded',1,2,10,?)",
            (run, thread, conversation, "milo-reply-" + run),
        )
        connection.execute("INSERT INTO run_messages VALUES (?,?,?)", (run, message, thread))
        connection.execute(
            "INSERT INTO revisit_intents "
            "(id,owner_id,conversation_id,origin_run_id,origin_index,state,"
            "due_at,initial_due_at,created_at,updated_at) VALUES (?,?,?,?,0,'scheduled',10,10,2,2)",
            (revisit, owner, conversation, run),
        )
        connection.execute("INSERT INTO revisit_sources VALUES (?,?)", (revisit, message))
        connection.execute(
            "INSERT INTO message_decisions VALUES (?,1,?,'deferred',2,NULL,?)",
            (message, run, revisit),
        )
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
        state = tuple(connection.execute("SELECT * FROM message_reply_state").fetchone())
    copy_path = tmp_path / "v29-copy.sqlite3"
    database.backup(copy_path)
    copied = Database(copy_path)
    copied.initialize()
    with copied.snapshot() as connection:
        for table, rows in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == rows
        assert tuple(connection.execute("SELECT * FROM message_reply_state").fetchone()) == state
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM revisit_claims").fetchone()[0] == 0
    with database.snapshot() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 28

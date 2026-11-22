"""v25 rows and sealed replies survive additive v26 migration unchanged."""

import json
from importlib.resources import files
from uuid import uuid4

from milo_home.automatic_services import AutomaticServices
from milo_home.backup_rehearsal import rehearse_backup
from milo_home.database import CURRENT_SCHEMA_VERSION, MIGRATIONS, Database
from milo_home.messages import MessageService
from milo_home.reply_receipts import publish_receipt


def test_v25_copy_keeps_every_old_row_and_does_not_invent_read_decisions(tmp_path):
    path = tmp_path / "v25.sqlite3"
    path.touch(mode=0o600)
    source = Database(path)
    owner, thread, first, second, run, reply = [str(uuid4()) for _ in range(6)]
    with source.connect() as connection:
        for number, name in MIGRATIONS:
            if number <= 25:
                connection.executescript(
                    files("milo_home").joinpath("migrations", name).read_text()
                )
        connection.execute("PRAGMA user_version=25")
        connection.execute(
            "INSERT INTO users VALUES (?, 'old', '合成旧账号', 'unused', 1)", (owner,)
        )
        connection.execute("INSERT INTO threads VALUES (?,?,1)", (thread, owner))
        for sequence, identity in enumerate((first, second), 1):
            connection.execute(
                "INSERT INTO messages "
                "(id,thread_id,sequence,author_id,role,body,client_key,created_at) "
                "VALUES (?,?,?,?,'visitor','合成旧留言',?,1)",
                (identity, thread, sequence, owner, str(uuid4())),
            )
        connection.execute(
            "INSERT INTO runs "
            "(id,thread_id,status,created_at,started_at,ended_at,deadline_at,unit_name) "
            "VALUES (?,?,'succeeded',2,2,3,100,?)",
            (run, thread, "milo-reply-" + run),
        )
        connection.execute("INSERT INTO run_messages VALUES (?,?,?)", (run, first, thread))
        connection.execute("INSERT INTO reply_run_scopes VALUES (?,?)", (run, owner))
        body = "合成旧回信。"
        result = json.dumps({"replies": [{"message_id": first, "body": body}]})
        connection.execute(
            "INSERT INTO reply_execution_results VALUES (?,3,'synthetic-old',?)", (run, result)
        )
        connection.execute(
            "INSERT INTO messages (id,thread_id,sequence,role,body,in_reply_to,run_id,created_at) "
            "VALUES (?,?,3,'milo',?,?,?,3)",
            (reply, thread, body, first, run),
        )
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
    copied = tmp_path / "v26.sqlite3"
    report = rehearse_backup(path, copied)
    assert (
        report["copied_schema_version"] == 25 and report["schema_version"] == CURRENT_SCHEMA_VERSION
    )
    restored = Database(copied)
    with restored.snapshot() as connection:
        for table, rows in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == rows
        states = {
            row["message_id"]: row["state"]
            for row in connection.execute("SELECT * FROM message_reply_state")
        }
        assert states == {first: "replied", second: "unknown"}
        assert connection.execute("SELECT count(*) FROM message_intake").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM message_decisions").fetchone()[0] == 0
    with source.snapshot() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 25
    AutomaticServices(restored).configure("reply", owner, "milo-home-reply.timer")
    assert publish_receipt(restored, owner, run)
    assert publish_receipt(restored, owner, run)
    new, _ = MessageService(restored).post(owner, thread, "迁移后新增。", str(uuid4()))
    with restored.snapshot() as connection:
        assert (
            connection.execute(
                "SELECT state FROM message_reply_state WHERE message_id=?", (new["id"],)
            ).fetchone()[0]
            == "unprocessed"
        )

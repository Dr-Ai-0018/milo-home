import time
from uuid import uuid4

from .database import Database
from .errors import ApiError
from .reception import on_new_input


class MessageService:
    def __init__(self, database: Database):
        self.database = database

    def list_threads(self, user_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT threads.id, threads.created_at, "
                "(SELECT count(*) FROM message_reply_state AS incoming "
                "WHERE incoming.thread_id = threads.id "
                "AND incoming.state IN ('unprocessed','unknown','deferred')) AS pending_count "
                "FROM threads WHERE owner_id = ? ORDER BY created_at, id",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_messages(self, user_id: str, thread_id: str, after: int, limit: int) -> dict:
        with self.database.connect() as connection:
            self._require_owner(connection, user_id, thread_id)
            rows = connection.execute(
                "SELECT * FROM messages WHERE thread_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (thread_id, after, limit),
            ).fetchall()
        return {
            "messages": [dict(row) for row in rows],
            "next_after": rows[-1]["sequence"] if rows else after,
        }

    def post(self, user_id: str, thread_id: str, body: str, key: str) -> tuple[dict, bool]:
        with self.database.connect(write=True) as connection:
            self._require_owner(connection, user_id, thread_id)
            conversation_id = connection.execute(
                "SELECT id FROM conversations WHERE thread_id = ? AND is_default = 1",
                (thread_id,),
            ).fetchone()[0]
            return self.insert(connection, user_id, thread_id, conversation_id, body, key)

    @staticmethod
    def insert(connection, user_id, thread_id, conversation_id, body, key):
        if not connection.in_transaction:
            raise ValueError("Message insert requires an existing transaction")
        previous = connection.execute(
            "SELECT * FROM messages WHERE author_id = ? AND client_key = ?", (user_id, key)
        ).fetchone()
        if previous:
            if (
                previous["thread_id"] != thread_id
                or previous["conversation_id"] != conversation_id
                or previous["body"] != body
            ):
                raise ApiError(409, "idempotency_conflict", "请求标识已用于另一条留言。")
            return dict(previous), False
        sequence = connection.execute(
            "SELECT coalesce(max(sequence), 0) + 1 FROM messages WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()[0]
        message_id = str(uuid4())
        now = int(time.time())
        connection.execute(
            "INSERT INTO messages "
            "(id, thread_id, sequence, author_id, role, body, client_key, created_at, "
            "conversation_id) VALUES (?, ?, ?, ?, 'visitor', ?, ?, ?, ?)",
            (
                message_id,
                thread_id,
                sequence,
                user_id,
                body,
                key,
                now,
                conversation_id,
            ),
        )
        on_new_input(connection, user_id, "reply", now)
        message = connection.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return dict(message), True

    @staticmethod
    def _require_owner(connection, user_id: str, thread_id: str):
        if not connection.execute(
            "SELECT 1 FROM threads WHERE id = ? AND owner_id = ?", (thread_id, user_id)
        ).fetchone():
            raise ApiError(404, "thread_not_found", "没有找到这段对话。")

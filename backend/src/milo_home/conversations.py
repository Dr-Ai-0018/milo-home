import time
from uuid import uuid4

from .errors import ApiError
from .messages import MessageService
from .reply_decisions import conversation_views, message_views

VIEW_SELECT = (
    "SELECT conversations.id, conversations.thread_id, conversations.kind, conversations.title, "
    "conversations.is_default, conversations.created_at, "
    "(SELECT count(*) FROM messages WHERE conversation_id = conversations.id) AS message_count, "
    "(SELECT created_at FROM messages WHERE conversation_id = conversations.id "
    "ORDER BY sequence DESC LIMIT 1) AS last_message_at, "
    "(SELECT role FROM messages WHERE conversation_id = conversations.id "
    "ORDER BY sequence DESC LIMIT 1) AS last_message_role, "
    "(SELECT created_at FROM messages WHERE conversation_id = conversations.id AND role = 'milo' "
    "ORDER BY sequence DESC LIMIT 1) AS last_reply_at, "
    "(SELECT count(*) FROM message_reply_state AS incoming "
    "WHERE incoming.conversation_id = conversations.id "
    "AND incoming.state IN ('unprocessed','unknown','deferred')) "
    "AS pending_count FROM conversations "
)


class ConversationService:
    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock

    def list(self, owner: str, thread_id: str) -> list[dict]:
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            MessageService._require_owner(connection, owner, thread_id)
            rows = connection.execute(
                VIEW_SELECT + "WHERE thread_id = ? ORDER BY created_at, id", (thread_id,)
            ).fetchall()
            return conversation_views(connection, rows)

    def create_letter(self, owner: str, thread_id: str, title: str | None, key: str):
        with self.database.connect(write=True) as connection:
            MessageService._require_owner(connection, owner, thread_id)
            previous = connection.execute(
                VIEW_SELECT + "WHERE thread_id = ? AND client_key = ?", (thread_id, key)
            ).fetchone()
            if previous:
                if previous["title"] != title:
                    raise ApiError(409, "idempotency_conflict", "请求标识已用于另一封信。")
                return conversation_views(connection, [previous])[0], False
            conversation_id = str(uuid4())
            connection.execute(
                "INSERT INTO conversations (id, thread_id, kind, title, client_key, created_at) "
                "VALUES (?, ?, 'letter', ?, ?, ?)",
                (conversation_id, thread_id, title, key, int(self.clock())),
            )
            return self._view(connection, conversation_id), True

    def chat(self, owner: str, thread_id: str) -> dict:
        with self.database.connect(write=True) as connection:
            MessageService._require_owner(connection, owner, thread_id)
            row = connection.execute(
                VIEW_SELECT + "WHERE thread_id = ? AND kind = 'chat'", (thread_id,)
            ).fetchone()
            if row:
                return conversation_views(connection, [row])[0]
            conversation_id = str(uuid4())
            connection.execute(
                "INSERT INTO conversations (id, thread_id, kind, created_at) "
                "VALUES (?, ?, 'chat', ?)",
                (conversation_id, thread_id, int(self.clock())),
            )
            return self._view(connection, conversation_id)

    def list_messages(
        self,
        owner: str,
        conversation_id: str,
        after: int | None,
        limit: int,
        *,
        before: int | None = None,
        latest: bool = False,
    ):
        if sum((after is not None, before is not None, latest)) > 1:
            raise ApiError(422, "pagination_conflict", "只能选择一种历史读取方向。")
        reverse = latest or before is not None
        cursor = before if reverse else after or 0
        comparison = "<" if reverse else ">"
        order = "DESC" if reverse else "ASC"
        predicate = f"AND sequence {comparison} ? " if cursor is not None else ""
        parameters = [conversation_id, *([] if cursor is None else [cursor]), limit]
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            self._require_owner(connection, owner, conversation_id)
            rows = connection.execute(
                "SELECT * FROM messages WHERE conversation_id = ? "
                + predicate
                + f"ORDER BY sequence {order} LIMIT ?",
                parameters,
            ).fetchall()
            oldest = min(row["sequence"] for row in rows) if rows else None
            has_more = oldest is not None and bool(
                connection.execute(
                    "SELECT 1 FROM messages WHERE conversation_id = ? AND sequence < ? LIMIT 1",
                    (conversation_id, oldest),
                ).fetchone()
            )
            projected = message_views(connection, rows)
        return {
            "messages": projected,
            "next_after": max(row["sequence"] for row in rows) if rows else after or 0,
            "next_before": oldest,
            "has_more_before": has_more,
        }

    def post(self, owner: str, conversation_id: str, body: str, key: str):
        with self.database.connect(write=True) as connection:
            conversation = self._require_owner(connection, owner, conversation_id)
            message, created = MessageService.insert(
                connection, owner, conversation["thread_id"], conversation_id, body, key
            )
            return message_views(connection, [message])[0], created

    @staticmethod
    def _require_owner(connection, owner, conversation_id):
        row = connection.execute(
            "SELECT conversations.* FROM conversations JOIN threads "
            "ON threads.id = conversations.thread_id "
            "WHERE conversations.id = ? AND threads.owner_id = ?",
            (conversation_id, owner),
        ).fetchone()
        if not row:
            raise ApiError(404, "conversation_not_found", "没有找到这段交流。")
        return row

    @staticmethod
    def _view(connection, conversation_id):
        row = connection.execute(VIEW_SELECT + "WHERE id = ?", (conversation_id,)).fetchone()
        return conversation_views(connection, [row])[0]

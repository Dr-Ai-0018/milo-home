"""Explicit account selection and body-free views of unanswered messages."""

from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, Field

from .errors import ApiError


@dataclass(frozen=True)
class ReplyScope:
    owner_id: str

    def __post_init__(self):
        if str(UUID(self.owner_id)) != self.owner_id:
            raise ValueError("An explicit canonical account UUID is required")

    def thread_id(self, connection) -> str:
        row = connection.execute(
            "SELECT threads.id FROM threads JOIN users ON users.id=threads.owner_id "
            "WHERE threads.owner_id=?",
            (self.owner_id,),
        ).fetchone()
        if row is None:
            raise ApiError(409, "reply_owner_missing", "未找到明确登记的回信账号与会话。")
        return row[0]

    def require_thread(self, connection, thread_id: str):
        if self.thread_id(connection) != thread_id:
            raise ApiError(409, "reply_scope_changed", "回信范围与登记账号不符。")


class ReplyQueueCounts(BaseModel):
    pending_count: int = Field(ge=0)
    available_count: int = Field(ge=0)
    reserved_count: int = Field(ge=0)
    retry_wait_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    oldest_pending_at: int | None
    retry_after: int | None
    clock_invalid: bool


def managed_scope_valid(connection, run):
    row = connection.execute(
        "SELECT owner_id FROM reply_run_scopes WHERE run_id=?", (run["id"],)
    ).fetchone()
    if row is None:
        return True
    return bool(
        connection.execute(
            "SELECT 1 FROM threads JOIN automatic_services ON automatic_services.owner_id="
            "threads.owner_id WHERE threads.id=? AND threads.owner_id=? "
            "AND automatic_services.kind='reply'",
            (run["thread_id"], row["owner_id"]),
        ).fetchone()
    )


def queue_counts(connection, thread_id: str, now: int, *, conversation_id=None):
    if not connection.in_transaction:
        raise ValueError("Reply queue inspection requires a snapshot transaction")
    scope = ""
    parameters = [now, thread_id]
    if conversation_id is not None:
        scope = " AND incoming.conversation_id=?"
        parameters.append(conversation_id)
    row = connection.execute(
        "WITH pending AS (SELECT incoming.created_at,delivery.retry_after,CASE "
        "WHEN incoming.state='deferred' THEN 'deferred' "
        "WHEN delivery.run_id IS NOT NULL THEN 'reserved' "
        "WHEN delivery.blocked_reason IS NOT NULL OR (delivery.attempts>=3 AND NOT EXISTS "
        "(SELECT 1 FROM reception_returned_attempts WHERE event_id=event.id)) THEN 'blocked' "
        "WHEN delivery.retry_after>? THEN 'retry_wait' ELSE 'available' END AS state "
        "FROM message_reply_state AS incoming "
        "LEFT JOIN wake_events AS event ON event.message_id=incoming.message_id "
        "LEFT JOIN wake_event_delivery AS delivery ON delivery.event_id=event.id "
        "WHERE incoming.state IN ('unprocessed','unknown','deferred') "
        "AND incoming.thread_id=?" + scope + ") "
        "SELECT count(*) AS pending_count,coalesce(sum(state='available'),0) AS available_count,"
        "coalesce(sum(state='reserved'),0) AS reserved_count,"
        "coalesce(sum(state='retry_wait'),0) AS retry_wait_count,"
        "coalesce(sum(state='blocked'),0) AS blocked_count,min(created_at) AS oldest_pending_at,"
        "min(CASE WHEN state='retry_wait' THEN retry_after END) AS retry_after,"
        "coalesce(max(created_at)>?,0) AS clock_invalid FROM pending",
        [*parameters, now],
    ).fetchone()
    return ReplyQueueCounts.model_validate(dict(row))

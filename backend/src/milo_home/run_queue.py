from uuid import uuid4

from .errors import ApiError
from .execution_budget import reserve_budget
from .execution_control import execution_busy
from .knock_delivery import freeze_knock
from .legacy_permits import legacy_schedule_open
from .reception import freeze_run, require_reception, settle_due
from .revisit_claims import claim, sources
from .revisit_queue import blocked_by


def queue_run(
    connection,
    thread_id: str,
    timeout: int,
    now: int,
    message_ids=None,
    *,
    require_policy=False,
    revisit=None,
) -> str:
    """Insert one frozen run; caller must hold a write transaction."""
    if not connection.in_transaction:
        raise ValueError("Run queue requires an existing transaction")
    if not 1 <= timeout <= 3600:
        raise ValueError("Run timeout must be 1–3600 seconds")
    if connection.execute("SELECT paused FROM schedule_state WHERE id = 1").fetchone()[0]:
        raise ApiError(409, "schedule_paused", "读信已暂停，请先恢复安排。")
    thread = connection.execute("SELECT owner_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    if not thread:
        raise ApiError(404, "thread_not_found", "没有找到这段对话。")
    if execution_busy(connection):
        raise ApiError(409, "run_busy", "已有运行占用，请先等待结束或执行恢复。")
    scope = ""
    parameters = [thread_id]
    if message_ids is not None:
        if not (0 if revisit else 1) <= len(message_ids) <= 10 or len(set(message_ids)) != len(
            message_ids
        ):
            raise ValueError("Invalid run message scope")
        scope = "AND incoming.message_id IN (" + ",".join("?" for _ in message_ids) + ") "
        parameters.extend(message_ids)
    selection_sql = (
        "SELECT incoming.message_id AS id, incoming.conversation_id "
        "FROM message_reply_state AS incoming "
        "WHERE incoming.thread_id = ? "
        "AND incoming.state IN ('unprocessed','unknown') " + scope
    )
    selected = connection.execute(
        selection_sql + "ORDER BY incoming.sequence LIMIT 10", parameters
    ).fetchall()
    if message_ids is not None and len(selected) != len(message_ids):
        raise ApiError(409, "scope_changed", "候选留言已变化，请重新整理。")
    if not selected and revisit is None:
        raise ApiError(409, "nothing_pending", "没有待回信的留言。")
    intent = (
        revisit_intent(connection, thread_id, thread["owner_id"], revisit, selected, now)
        if revisit
        else None
    )
    conversation_id = intent["conversation_id"] if intent else selected[0]["conversation_id"]
    if message_ids is not None and any(
        row["conversation_id"] != conversation_id for row in selected
    ):
        raise ApiError(409, "scope_changed", "候选留言不属于同一会话，请重新整理。")
    if message_ids is None and revisit is None:
        selected = connection.execute(
            selection_sql + "AND incoming.conversation_id = ? ORDER BY incoming.sequence LIMIT 10",
            [*parameters, conversation_id],
        ).fetchall()
    if not legacy_schedule_open(connection, now):
        raise ApiError(409, "work_schedule_closed", "当前只收件，尚未到工作时间。")
    settle_due(connection, now, owner=thread["owner_id"])
    require_reception(connection, thread["owner_id"], "reply", now)
    timeout = reserve_budget(connection, now, timeout, required=require_policy)
    return insert_run(
        connection,
        thread_id,
        conversation_id,
        selected,
        timeout,
        now,
        thread["owner_id"],
        revisit=revisit,
    )


def insert_run(
    connection, thread_id, conversation_id, selected, timeout, now, owner, *, revisit=None
):
    run_id = str(uuid4())
    connection.execute(
        "INSERT INTO runs (id, thread_id, status, created_at, deadline_at, unit_name, "
        "conversation_id) VALUES (?, ?, 'queued', ?, ?, ?, ?)",
        (run_id, thread_id, now, now + timeout, "milo-reply-" + run_id, conversation_id),
    )
    connection.execute("INSERT INTO legacy_launches VALUES (?,NULL)", (run_id,))
    connection.executemany(
        "INSERT INTO run_messages VALUES (?, ?, ?)",
        [(run_id, row["id"], thread_id) for row in selected],
    )
    freeze_knock(connection, run_id, conversation_id, now)
    freeze_run(connection, run_id, owner)
    if revisit is not None:
        claim(connection, run_id, revisit["id"], revisit["revision"], now)
    return run_id


def revisit_intent(connection, thread_id, owner, revisit, selected, now):
    intent = connection.execute(
        "SELECT v.* FROM revisit_intents v JOIN conversations c "
        "ON c.id=v.conversation_id WHERE v.id=? AND v.owner_id=? AND c.thread_id=?",
        (revisit["id"], owner, thread_id),
    ).fetchone()
    if (
        not intent
        or intent["revision"] != revisit["revision"]
        or intent["state"] not in {"scheduled", "blocked"}
    ):
        raise ApiError(409, "revisit_changed", "回访安排已经变化。")
    if intent["due_at"] > now or blocked_by(connection, intent, now):
        raise ApiError(409, "revisit_not_ready", "回访还未到时或当前条件尚未开放。")
    if len(selected) + len(sources(connection, intent["id"])) > 10:
        raise ValueError("Combined revisit and new-input scope exceeds the delivery limit")
    return intent

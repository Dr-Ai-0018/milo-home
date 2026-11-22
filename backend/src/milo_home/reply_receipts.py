"""Sealed success evidence permits publication recovery without another model call."""

import time

from .automatic_guard import guard_automatic
from .errors import ApiError
from .legacy_permits import guard_legacy_budget
from .management_lease import current_boot
from .reply_projection_evidence import read_projection, require_publication
from .reply_publication import publish_rows
from .reply_results import parse_result, require_scope
from .reply_scope import ReplyScope, managed_scope_valid
from .revisit_claims import source_ids


def seal_result(database, run_id, result, *, code, events, ended, clock=time.time):
    if code != 0 or events.completed != 1 or events.failed or events.invalid or not ended:
        raise ValueError("A result requires a successful turn and confirmed process-tree stop")
    return seal_confirmed_result(database, run_id, result, clock=clock)


def seal_confirmed_result(database, run_id, result, *, clock=time.time):
    """Internal publication gate; caller has verified its transport and whole-family stop."""
    now = int(clock())
    payload = result.model_dump_json()
    if len(payload.encode()) > 256 * 1024:
        raise ValueError("Reply evidence exceeds its bounded size")
    with database.connect(write=True) as connection:
        run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None or run["status"] != "running" or run["stop_reason"]:
            return False
        if not connection.execute(
            "SELECT 1 FROM reply_run_scopes WHERE run_id=?", (run_id,)
        ).fetchone():
            raise ValueError("Only a managed reply can seal automatic publication evidence")
        if not guard_legacy_budget(connection, run, now, require_launch=True):
            return False
        if not guard_automatic(connection, run, now, require_launch=True):
            return False
        ids = [
            row[0]
            for row in connection.execute(
                "SELECT message_id FROM run_messages WHERE run_id=?", (run_id,)
            )
        ]
        require_scope(result, ids, now, revisit_source_ids=source_ids(connection, run_id))
        connection.execute(
            "INSERT INTO reply_execution_results VALUES (?,?,?,?)",
            (run_id, now, current_boot(), payload),
        )
    return True


def publish_receipt(database, owner, run_id, *, clock=time.time):
    now = int(clock())
    with database.connect(write=True) as connection:
        run = connection.execute(
            "SELECT runs.* FROM runs JOIN reply_run_scopes ON reply_run_scopes.run_id=runs.id "
            "WHERE runs.id=? AND reply_run_scopes.owner_id=?",
            (run_id, owner),
        ).fetchone()
        if run is None:
            raise ApiError(404, "reply_run_missing", "没有找到本账号的独立回信运行。")
        ReplyScope(owner).require_thread(connection, run["thread_id"])
        if not managed_scope_valid(connection, run):
            raise ApiError(409, "reply_scope_changed", "原回信范围已经变化。")
        receipt = connection.execute(
            "SELECT * FROM reply_execution_results WHERE run_id=?", (run_id,)
        ).fetchone()
        if receipt is None:
            raise ApiError(409, "reply_result_unknown", "缺少已确认的完整结果，不能补投或重跑。")
        result = parse_result(receipt["result"])
        completed = receipt["completed_at"]
        if not run["created_at"] <= completed < run["deadline_at"] or now < completed:
            raise ValueError("Reply publication evidence has an invalid clock")
        if run["status"] == "succeeded":
            published = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM messages WHERE run_id=? AND role='milo'",
                    (run_id,),
                )
            ]
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT message_id FROM run_messages WHERE run_id=?", (run_id,)
                )
            ]
            require_publication(
                result, run, published, read_projection(connection, run_id), ids, completed
            )
            return True
        if run["status"] != "running" or run["stop_reason"]:
            return False
        # Schedule and budget were checked at the recorded successful completion.
        # Recovery publishes that result; it neither starts a turn nor charges downtime.
        publish_rows(connection, run, result, now, completed_at=completed)
    return True

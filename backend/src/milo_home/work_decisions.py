"""Persisted work choices, independent of the task's execution status."""

from .errors import ApiError
from .interaction_models import WorkDecisionView
from .interaction_timing import RevisitView

UNRESOLVED = ("scheduled", "running", "blocked", "needs_reconciliation")
SUSPENDED_SQL = (
    "EXISTS (SELECT 1 FROM work_task_decisions d WHERE d.task_id=work_tasks.id "
    "AND (d.state='needs_clarification' OR (d.state='deferred' AND EXISTS "
    "(SELECT 1 FROM work_revisits v WHERE v.id=d.revisit_id "
    "AND v.state NOT IN ('completed','cancelled')))))"
)


def latest(connection, task_id):
    return connection.execute(
        "SELECT * FROM work_task_decisions WHERE task_id=?", (task_id,)
    ).fetchone()


def revisit_view(row):
    return RevisitView.model_validate(
        {
            key: row[key]
            for key in ("id", "revision", "state", "due_at", "updated_at", "blocked_reason")
        }
    )


def decision_view(connection, task_id):
    row = latest(connection, task_id)
    if row is None:
        return None
    revisit = None
    if row["revisit_id"] is not None:
        saved = connection.execute(
            "SELECT * FROM work_revisits WHERE id=?", (row["revisit_id"],)
        ).fetchone()
        # A user's stop is not a new personal decision. Keep its history, while
        # the task's cancelled status explains why the old arrangement is over.
        if saved["state"] not in UNRESOLVED:
            return None
        revisit = revisit_view(saved)
    return WorkDecisionView(
        state=row["state"],
        revision=row["revision"],
        decided_at=row["decided_at"],
        receipt=row["receipt"],
        revisit=revisit,
    ).model_dump(mode="json")


def history_clause(group):
    if group == "active":
        return (
            "(status IN ('queued','running','stopping') OR (status='completed' AND "
            + SUSPENDED_SQL
            + "))"
        )
    if group == "finished":
        return (
            "(status IN ('failed','cancelled','timed_out') OR (status='completed' AND NOT "
            + SUSPENDED_SQL
            + "))"
        )
    if group == "needs_reconciliation":
        return "status='needs_reconciliation'"
    if group == "all":
        return "1"
    raise ValueError("Unknown work history group")


def clarification_allowed(connection, task):
    row = latest(connection, task["id"])
    return (
        row is not None
        and row["state"] == "needs_clarification"
        and task["status"] == "completed"
        and task["stop_requested_at"] is None
        and not connection.execute(
            "SELECT 1 FROM execution_windows WHERE task_id=? AND state!='closed'", (task["id"],)
        ).fetchone()
    )


def record_clarification(connection, task, input_id, now):
    if not clarification_allowed(connection, task):
        raise ApiError(409, "work_input_conflict", "任务状态已变化，补充内容尚未投递。")
    row = latest(connection, task["id"])
    connection.execute(
        "INSERT INTO work_continuations VALUES (?,?,?)", (input_id, task["id"], row["revision"])
    )
    connection.execute(
        "UPDATE work_tasks SET status='queued',updated_at=? WHERE id=?", (now, task["id"])
    )


def stop_suspended(connection, task, now):
    if task["status"] != "completed" or task["stop_requested_at"] is not None:
        return False
    row = latest(connection, task["id"])
    if row is None or row["state"] not in {"deferred", "needs_clarification"}:
        return False
    connection.execute(
        "UPDATE work_tasks SET status='cancelled',stop_requested_at=?,updated_at=? WHERE id=?",
        (now, now, task["id"]),
    )
    cancel_revisits(connection, task["id"], now)
    return True


def cancel_revisits(connection, task_id, now):
    connection.execute(
        "UPDATE work_revisits SET state='cancelled',blocked_reason=NULL,"
        "revision=revision+1,updated_at=? WHERE task_id=? "
        "AND state IN ('scheduled','blocked')",
        (now, task_id),
    )

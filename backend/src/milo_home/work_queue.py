"""One ready work source for explicit receivers and shared automatic arbitration."""

from .automatic_services import entry_available
from .errors import ApiError
from .execution_budget import reserve_budget
from .execution_control import execution_busy, working
from .reception import blocking_reason


def decisions_enabled(connection, owner):
    row = connection.execute(
        "SELECT enabled FROM work_decision_capabilities WHERE owner_id=?", (owner,)
    ).fetchone()
    return bool(row and row[0])


def next_work(connection, owner, session_id, access, now, *, decisions=False):
    scope = (owner, session_id, access["owner_revision"], access["workspace_revision"])
    rows = connection.execute(
        "SELECT t.id,t.sequence,t.created_at AS ready_at,d.state,d.revision FROM work_tasks t "
        "LEFT JOIN work_task_decisions d ON d.task_id=t.id "
        "WHERE t.owner_id=? AND t.session_id=? AND t.owner_revision=? AND t.workspace_revision=? "
        "AND t.status='queued' AND t.stop_requested_at IS NULL ORDER BY t.sequence LIMIT 32",
        scope,
    ).fetchall()
    candidates = []
    for row in rows:
        if row["state"] is None:
            candidates.append(
                {
                    "id": row["id"],
                    "ready_at": row["ready_at"],
                    "sequence": row["sequence"],
                }
            )
        elif decisions and row["state"] == "needs_clarification":
            continuation = connection.execute(
                "SELECT min(i.created_at) FROM work_continuations c "
                "JOIN work_inputs i ON i.id=c.input_id "
                "WHERE c.task_id=? AND c.decision_revision=? AND i.delivery='queued'",
                (row["id"], row["revision"]),
            ).fetchone()[0]
            if continuation is not None:
                candidates.append(
                    {
                        "id": row["id"],
                        "ready_at": continuation,
                        "sequence": row["sequence"],
                    }
                )
    if decisions:
        due = connection.execute(
            "SELECT t.id,t.sequence,v.due_at AS ready_at FROM work_tasks t "
            "JOIN work_task_decisions d ON d.task_id=t.id "
            "JOIN work_revisits v ON v.id=d.revisit_id "
            "WHERE t.owner_id=? AND t.session_id=? "
            "AND t.owner_revision=? AND t.workspace_revision=? "
            "AND t.status='completed' AND t.stop_requested_at IS NULL AND d.state='deferred' "
            "AND v.state IN ('scheduled','blocked') AND v.due_at<=? AND v.updated_at<=? "
            "ORDER BY v.due_at,v.id LIMIT 1",
            (*scope, now, now),
        ).fetchone()
        if due is not None:
            candidates.append(dict(due))
    return (
        min(candidates, key=lambda row: (row["ready_at"], row["sequence"], row["id"]))
        if candidates
        else None
    )


def observe_due(connection, owner, now, boot):
    entry = connection.execute(
        "SELECT * FROM automatic_services WHERE kind='work' AND owner_id=?", (owner,)
    ).fetchone()
    reason = None
    if not decisions_enabled(connection, owner) or not entry_available(
        entry, now, boot, caller=True
    ):
        reason = "service"
    elif not working(connection, now):
        reason = "schedule"
    elif blocking_reason(connection, owner, "work", now):
        reason = "reception"
    elif execution_busy(connection):
        reason = "occupancy"
    else:
        try:
            reserve_budget(connection, now, 1, required=True)
        except ApiError:
            reason = "budget"
    rows = connection.execute(
        "SELECT v.id FROM work_revisits v JOIN work_tasks t ON t.id=v.task_id "
        "WHERE t.owner_id=? AND v.state IN ('scheduled','blocked') "
        "AND v.due_at<=? AND v.updated_at<=? ORDER BY v.due_at,v.id LIMIT 1000",
        (owner, now, now),
    ).fetchall()
    connection.executemany(
        "UPDATE work_revisits SET state=?,blocked_reason=?,updated_at=? WHERE id=?",
        [("blocked" if reason else "scheduled", reason, now, row["id"]) for row in rows],
    )

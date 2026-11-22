from .reception import blocking_reason
from .reply_scope import managed_scope_valid
from .revisit_claims import claim_current
from .schedule_policy import phase_at
from .schedules import schedule_snapshot


def guard_automatic(connection, run, now: int, *, require_launch: bool = False) -> bool:
    """Recheck automatic work inside the caller's transaction; manual runs are unchanged."""
    dispatch = connection.execute(
        "SELECT * FROM wake_dispatches WHERE run_id = ?", (run["id"],)
    ).fetchone()
    if not dispatch:
        return True
    if run["status"] not in {"queued", "running", "stopping"}:
        return False
    if run["stop_reason"]:
        return False
    reason = None
    stop = "interrupted"
    policy, state = schedule_snapshot(connection)
    if not managed_scope_valid(connection, run):
        reason = "scope_changed"
    elif not claim_current(connection, run["id"]):
        reason = "revisit_changed"
        stop = "cancelled"
    elif now < run["created_at"] or (
        dispatch["launched_at"] is not None and now < dispatch["launched_at"]
    ):
        reason = "clock_rewind"
    elif now >= run["deadline_at"]:
        reason = "deadline"
        stop = "timed_out"
    elif state["paused"] or not policy or phase_at(policy, state, now)[0] != "working":
        reason = "schedule_closed"
        stop = "cancelled"
    elif dispatch["launched_at"] is None and now >= dispatch["lease_until"]:
        reason = "lease_expired"
    elif require_launch and dispatch["launched_at"] is None:
        reason = "launch_missing"
    if reason is None and dispatch["launched_at"] is None:
        owner = connection.execute(
            "SELECT owner_id FROM threads WHERE id=?", (run["thread_id"],)
        ).fetchone()[0]
        reason = blocking_reason(connection, owner, "reply", now)
        if reason:
            stop = "cancelled"
    if reason:
        connection.execute(
            "UPDATE runs SET status = 'stopping', stop_reason = coalesce(stop_reason, ?) "
            "WHERE id = ?",
            (stop, run["id"]),
        )
        connection.execute(
            "UPDATE wake_dispatches SET abort_reason = coalesce(abort_reason, ?) WHERE run_id = ?",
            (reason, run["id"]),
        )
        return False
    return not run["stop_reason"]

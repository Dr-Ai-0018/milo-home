from .wakeup_events import MAX_RESERVATIONS

SAFE_PRELAUNCH_ABORTS = {"lease_expired", "schedule_closed", "clock_rewind"}


def retry_is_safe(dispatch) -> bool:
    if dispatch["launched_at"] is not None or dispatch["failure_code"] == "configuration_error":
        return False
    if dispatch["abort_reason"] in SAFE_PRELAUNCH_ABORTS:
        return True
    return (
        dispatch["abort_reason"] is None
        and dispatch["status"] == "failed"
        and dispatch["failure_code"] == "interrupted"
    )


def reconcile(connection, now: int) -> dict:
    summary = {
        "settled_runs": 0,
        "completed_messages": 0,
        "retrying_messages": 0,
        "blocked_messages": 0,
    }
    rows = connection.execute(
        "SELECT wake_dispatches.*, runs.status, runs.failure_code FROM wake_dispatches "
        "JOIN runs ON runs.id = wake_dispatches.run_id WHERE settled_at IS NULL "
        "AND runs.status IN ('succeeded', 'failed', 'cancelled', 'timed_out') ORDER BY runs.rowid"
    ).fetchall()
    for dispatch in rows:
        deliveries = connection.execute(
            "SELECT wake_event_delivery.*, wake_events.kind FROM wake_event_delivery "
            "JOIN wake_events ON wake_events.id = wake_event_delivery.event_id WHERE run_id = ?",
            (dispatch["run_id"],),
        ).fetchall()
        for delivery in deliveries:
            retry_after = 0
            blocked = None
            reception_abort = dispatch["launched_at"] is None and dispatch["abort_reason"] in {
                "reception_closed",
                "reception_clock_invalid",
            }
            if dispatch["status"] == "succeeded":
                outcome = "completed_messages"
                connection.execute(
                    "UPDATE wake_events SET state = 'done' WHERE id = ?", (delivery["event_id"],)
                )
            elif reception_abort:
                # No delivery was authorized. Return only this reservation's slot;
                # the original counter and full dispatch audit remain intact.
                outcome = "retrying_messages"
                connection.execute(
                    "INSERT INTO reception_returned_attempts VALUES (?,?)",
                    (delivery["event_id"], dispatch["run_id"]),
                )
            elif retry_is_safe(dispatch) and delivery["attempts"] < MAX_RESERVATIONS:
                outcome = "retrying_messages"
                retry_after = now + 30 * delivery["attempts"]
            else:
                outcome = "blocked_messages"
                blocked = (
                    "retry_exhausted"
                    if retry_is_safe(dispatch)
                    else dispatch["failure_code"] or dispatch["status"]
                )
            connection.execute(
                "UPDATE wake_event_delivery SET run_id = NULL, retry_after = ?, blocked_reason = ? "
                "WHERE event_id = ? AND run_id = ?",
                (
                    retry_after,
                    blocked,
                    delivery["event_id"],
                    dispatch["run_id"],
                ),
            )
            if delivery["kind"] == "message":
                summary[outcome] += 1
        connection.execute(
            "UPDATE wake_dispatches SET settled_at = ? WHERE run_id = ? AND settled_at IS NULL",
            (now, dispatch["run_id"]),
        )
        summary["settled_runs"] += 1
    return summary

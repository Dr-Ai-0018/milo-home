"""One pre-spawn marker for every supported legacy execution path."""

from .execution_budget import held_budget_allows, shared_policy
from .reception import blocking_reason
from .schedule_policy import phase_at
from .schedules import schedule_snapshot


def legacy_schedule_open(connection, now):
    if shared_policy(connection) is None:
        return True
    policy, state = schedule_snapshot(connection)
    return policy is not None and phase_at(policy, state, now)[0] == "working"


def guard_legacy_budget(connection, run, now, *, require_launch=False):
    if shared_policy(connection) is None:
        return True
    allowed = held_budget_allows(connection, now) and legacy_schedule_open(connection, now)
    if require_launch:
        allowed = (
            allowed
            and connection.execute(
                "SELECT 1 FROM legacy_launches WHERE run_id=? AND authorized_at IS NOT NULL",
                (run["id"],),
            ).fetchone()
            is not None
        )
    if not allowed:
        connection.execute(
            "UPDATE runs SET status='stopping',stop_reason=coalesce(stop_reason,'cancelled') "
            "WHERE id=? AND status IN ('queued','running','stopping')",
            (run["id"],),
        )
    return allowed


def authorize_legacy_launch(connection, run_id, now):
    run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if (
        run is None
        or run["status"] != "queued"
        or run["stop_reason"] is not None
        or now < run["created_at"]
        or now >= run["deadline_at"]
        or connection.execute("SELECT paused FROM schedule_state WHERE id=1").fetchone()[0]
        or connection.execute(
            "SELECT 1 FROM legacy_launches WHERE run_id=? AND authorized_at IS NOT NULL", (run_id,)
        ).fetchone()
        or not guard_legacy_budget(connection, run, now)
    ):
        return False
    owner = connection.execute(
        "SELECT owner_id FROM threads WHERE id=?", (run["thread_id"],)
    ).fetchone()[0]
    if blocking_reason(connection, owner, "reply", now):
        connection.execute(
            "UPDATE runs SET status='stopping',stop_reason='cancelled' WHERE id=?", (run_id,)
        )
        return False
    connection.execute(
        "INSERT INTO legacy_launches VALUES (?,?) ON CONFLICT(run_id) DO UPDATE "
        "SET authorized_at=excluded.authorized_at WHERE authorized_at IS NULL",
        (run_id, now),
    )
    return True

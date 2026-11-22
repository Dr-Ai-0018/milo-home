"""One daily allowance projection over legacy history and new execution records."""

import time
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from .errors import ApiError
from .schedule_policy import BEIJING, day_start


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    daily_launches: int | None = Field(ge=1, le=2**31 - 1)
    daily_seconds: int | None = Field(ge=1, le=2**31 - 1)


def shared_policy(connection):
    row = connection.execute("SELECT policy FROM execution_policy WHERE id=1").fetchone()
    return ExecutionPolicy.model_validate_json(row[0]) if row[0] is not None else None


def _legacy_usage(connection, start, end):
    rows = connection.execute(
        "SELECT runs.*,wake_dispatches.run_id AS automatic_id,"
        "wake_dispatches.launched_at,legacy_launches.run_id AS recorded_id,"
        "legacy_launches.authorized_at FROM runs "
        "LEFT JOIN wake_dispatches ON wake_dispatches.run_id=runs.id "
        "LEFT JOIN legacy_launches ON legacy_launches.run_id=runs.id "
        "WHERE runs.created_at>=? AND runs.created_at<?",
        (start, end),
    ).fetchall()
    launches = seconds = 0
    for row in rows:
        active = row["status"] in {"queued", "running", "stopping"}
        launched = any(
            row[key] is not None for key in ["authorized_at", "launched_at", "started_at"]
        )
        # Historical manual runs have no pre-send marker. Only a configuration
        # failure is known to precede execution; other missing evidence is charged.
        uncertain = (
            row["automatic_id"] is None
            and row["recorded_id"] is None
            and row["failure_code"] != "configuration_error"
        )
        if not (active or launched or uncertain):
            continue
        launches += 1
        duration = max(1, (row["deadline_at"] or row["created_at"] + 3600) - row["created_at"])
        latest_launch = max(
            row[key] or row["created_at"] for key in ["authorized_at", "launched_at", "started_at"]
        )
        if active or row["ended_at"] is None or row["ended_at"] < latest_launch:
            seconds += duration
        else:
            seconds += min(duration, max(1, row["ended_at"] - row["created_at"]))
    return launches, seconds


def _window_usage(connection, start, end):
    launches = connection.execute(
        "SELECT count(*) FROM execution_permits WHERE created_at>=? AND created_at<? "
        "AND state!='cancelled'",
        (start, end),
    ).fetchone()[0]
    rows = connection.execute(
        "SELECT execution_windows.*,EXISTS (SELECT 1 FROM execution_permits "
        "WHERE window_id=execution_windows.id AND launched_at IS NOT NULL) AS launched "
        "FROM execution_windows WHERE created_at>=? AND created_at<?",
        (start, end),
    ).fetchall()
    seconds = 0
    for row in rows:
        duration = row["deadline_at"] - row["created_at"]
        if row["state"] != "closed":
            seconds += duration
        elif row["launched"]:
            seconds += min(duration, max(1, row["ended_at"] - row["created_at"]))
    return launches, seconds


def latest_execution_time(connection):
    return connection.execute(
        "SELECT max(stamp) FROM (SELECT max(created_at) AS stamp FROM runs "
        "UNION ALL SELECT max(ended_at) FROM runs "
        "UNION ALL SELECT max(started_at) FROM runs "
        "UNION ALL SELECT max(launched_at) FROM wake_dispatches "
        "UNION ALL SELECT max(authorized_at) FROM legacy_launches "
        "UNION ALL SELECT max(created_at) FROM execution_windows "
        "UNION ALL SELECT max(ended_at) FROM execution_windows "
        "UNION ALL SELECT max(created_at) FROM execution_permits "
        "UNION ALL SELECT max(launched_at) FROM execution_permits "
        "UNION ALL SELECT max(ended_at) FROM execution_permits "
        "UNION ALL SELECT updated_at FROM execution_policy)"
    ).fetchone()[0]


def shared_budget_view(connection, policy, now: int, timeout: int) -> dict:
    if not connection.in_transaction:
        raise ValueError("Budget inspection requires a transaction")
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError("Execution timeout must be 1–3600 seconds")
    day = datetime.fromtimestamp(now, BEIJING).date()
    start, end = day_start(day), day_start(day + timedelta(days=1))
    latest = latest_execution_time(connection)
    old_launches, old_seconds = _legacy_usage(connection, start, end)
    new_launches, new_seconds = _window_usage(connection, start, end)
    launches, seconds = old_launches + new_launches, old_seconds + new_seconds
    remaining_launches = None if policy.daily_launches is None else policy.daily_launches - launches
    remaining_seconds = None if policy.daily_seconds is None else policy.daily_seconds - seconds
    allowed = min(timeout, end - now)
    if remaining_seconds is not None:
        allowed = min(allowed, max(0, remaining_seconds))
    reason = None
    if latest is not None and now < latest:
        reason = "budget_clock_rewind"
    elif remaining_launches is not None and remaining_launches <= 0:
        reason = "daily_budget_exhausted"
    elif allowed < 1:
        reason = "daily_budget_exhausted"
    return {
        "day": day.isoformat(),
        "timezone": "Asia/Shanghai",
        "held_or_charged_launches": launches,
        "held_or_charged_seconds": seconds,
        "remaining_launches": remaining_launches,
        "remaining_seconds": remaining_seconds,
        "next_timeout": 0 if reason else allowed,
        "blocking_reason": reason,
    }


def held_budget_allows(connection, now: int) -> bool:
    policy = shared_policy(connection)
    if policy is None:
        return True
    view = shared_budget_view(connection, policy, now, 1)
    return (
        view["blocking_reason"] != "budget_clock_rewind"
        and (view["remaining_launches"] is None or view["remaining_launches"] >= 0)
        and (view["remaining_seconds"] is None or view["remaining_seconds"] >= 0)
    )


def reserve_budget(connection, now: int, timeout: int, *, required=False) -> int:
    policy = shared_policy(connection)
    if policy is None:
        if required:
            raise ApiError(409, "execution_policy_missing", "请先明确共同执行额度。")
        return timeout
    view = shared_budget_view(connection, policy, now, timeout)
    if view["blocking_reason"]:
        raise ApiError(409, view["blocking_reason"], "共同执行额度已用完或时钟异常。")
    return view["next_timeout"]


class ExecutionBudget:
    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock

    def configure(self, policy: ExecutionPolicy):
        if not isinstance(policy, ExecutionPolicy):
            raise ValueError("Explicit execution policy required")
        with self.database.connect(write=True) as connection:
            now = int(self.clock())
            previous = connection.execute("SELECT updated_at FROM execution_policy").fetchone()[0]
            if now < previous:
                raise ValueError("Policy clock moved backwards")
            connection.execute(
                "UPDATE execution_policy SET policy=?,revision=revision+1,updated_at=? WHERE id=1",
                (policy.model_dump_json(), now),
            )

    def view(self, timeout=3600):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            policy = shared_policy(connection)
            if policy is None:
                raise ApiError(409, "execution_policy_missing", "请先明确共同执行额度。")
            return shared_budget_view(connection, policy, int(self.clock()), timeout)

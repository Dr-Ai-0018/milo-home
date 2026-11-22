"""Daily execution allowances derived from durable dispatch/run history."""

import os
import stat
from datetime import datetime, timedelta
from pathlib import Path

from .execution_budget import ExecutionPolicy as AutomaticPolicy
from .execution_budget import shared_budget_view, shared_policy
from .schedule_policy import BEIJING, day_start


def load_policy(path: Path) -> AutomaticPolicy:
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Policy must be an absolute non-linked path")
    metadata = path.stat()
    parent = path.parent.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o077
        or metadata.st_size > 8192
    ):
        raise ValueError("Policy and parent must be private and owned by the caller")
    return AutomaticPolicy.model_validate_json(path.read_bytes())


def budget_view(connection, policy: AutomaticPolicy, now: int, timeout: int) -> dict:
    if not connection.in_transaction:
        raise ValueError("Budget inspection requires a transaction")
    common = shared_policy(connection)
    if common is not None:
        return shared_budget_view(connection, common, now, timeout)
    day = datetime.fromtimestamp(now, BEIJING).date()
    start = day_start(day)
    end = day_start(day + timedelta(days=1))
    latest = connection.execute(
        "SELECT max(max(runs.created_at, coalesce(runs.ended_at, 0), "
        "coalesce(wake_dispatches.launched_at, 0))) FROM runs "
        "JOIN wake_dispatches ON wake_dispatches.run_id = runs.id"
    ).fetchone()[0]
    usage = connection.execute(
        "SELECT coalesce(sum(CASE WHEN launched_at IS NOT NULL "
        "OR status IN ('queued', 'running', 'stopping') THEN 1 ELSE 0 END), 0) AS launches, "
        "coalesce(sum(CASE "
        "WHEN status IN ('queued', 'running', 'stopping') THEN deadline_at - created_at "
        "WHEN launched_at IS NULL THEN 0 "
        "WHEN ended_at IS NULL OR ended_at < coalesce(launched_at, created_at) "
        "THEN deadline_at - created_at "
        "ELSE min(deadline_at - created_at, max(1, ended_at - created_at)) END), 0) AS seconds "
        "FROM runs JOIN wake_dispatches ON wake_dispatches.run_id = runs.id "
        "WHERE created_at >= ? AND created_at < ?",
        (start, end),
    ).fetchone()
    remaining_launches = (
        None if policy.daily_launches is None else policy.daily_launches - usage["launches"]
    )
    remaining_seconds = (
        None if policy.daily_seconds is None else policy.daily_seconds - usage["seconds"]
    )
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
        "held_or_charged_launches": usage["launches"],
        "held_or_charged_seconds": usage["seconds"],
        "remaining_launches": remaining_launches,
        "remaining_seconds": remaining_seconds,
        "next_timeout": 0 if reason else allowed,
        "blocking_reason": reason,
    }


def launch_budget_allows(connection, policy: AutomaticPolicy, now: int) -> bool:
    # This reservation already holds its own call/time allowance. Zero remaining
    # is allowed here; negative remaining means another path or policy used it.
    view = budget_view(connection, policy, now, 1)
    return (
        view["blocking_reason"] != "budget_clock_rewind"
        and (view["remaining_launches"] is None or view["remaining_launches"] >= 0)
        and (view["remaining_seconds"] is None or view["remaining_seconds"] >= 0)
    )

"""Read-only operational counts, deliberately separate from billing or budgets."""

import time
from datetime import date, datetime, timedelta
from datetime import time as day_time
from pathlib import Path

from .database import CURRENT_SCHEMA_VERSION
from .database_inspection import readonly_snapshot
from .schedule_policy import BEIJING


def inspect_usage(path: Path, day: date | None = None, *, clock=time.time) -> dict:
    now = int(clock())
    day = day or datetime.fromtimestamp(now, BEIJING).date()
    start = int(datetime.combine(day, day_time(), BEIJING).timestamp())
    end = int(datetime.combine(day + timedelta(days=1), day_time(), BEIJING).timestamp())
    with readonly_snapshot(path) as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0] != CURRENT_SCHEMA_VERSION:
            raise ValueError("Usage inspection requires the current schema")
        reservations = connection.execute(
            "SELECT count(*) FROM wake_dispatches JOIN runs ON runs.id = wake_dispatches.run_id "
            "WHERE runs.created_at >= ? AND runs.created_at < ?",
            (start, end),
        ).fetchone()[0]
        launches = connection.execute(
            "SELECT count(*) FROM wake_dispatches WHERE launched_at >= ? AND launched_at < ?",
            (start, end),
        ).fetchone()[0]
        statuses = [
            dict(row)
            for row in connection.execute(
                "SELECT runs.status, count(*) AS count FROM runs "
                "JOIN wake_dispatches ON wake_dispatches.run_id = runs.id "
                "WHERE runs.created_at >= ? AND runs.created_at < ? "
                "GROUP BY runs.status ORDER BY runs.status",
                (start, end),
            )
        ]
        active = connection.execute(
            "SELECT count(*) AS count, "
            "coalesce(sum(runs.created_at < ?), 0) AS carried_from_earlier_days "
            "FROM runs JOIN wake_dispatches ON wake_dispatches.run_id = runs.id "
            "WHERE runs.status IN ('queued', 'running', 'stopping')",
            (start,),
        ).fetchone()
        clock_ahead = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM runs "
            "JOIN wake_dispatches ON wake_dispatches.run_id = runs.id "
            "WHERE runs.created_at > ? OR wake_dispatches.launched_at > ?)",
            (now, now),
        ).fetchone()[0]
    return {
        "observed_at": now,
        "day": day.isoformat(),
        "timezone": "Asia/Shanghai",
        "automatic_wakeup": False,
        "automatic_reservations_created": reservations,
        "automatic_launch_permissions_recorded": launches,
        "reservation_statuses": statuses,
        "current_active_automatic_runs": dict(active),
        "clock_ahead": bool(clock_ahead),
        "tokens": None,
        "cost": None,
        "billing_status": "unavailable",
        "daily_budget_enforced": False,
    }

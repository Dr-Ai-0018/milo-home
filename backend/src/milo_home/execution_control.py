"""Finite windows and shared occupancy; turn permits remain separate."""

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

from .errors import ApiError
from .execution_budget import held_budget_allows, latest_execution_time, reserve_budget
from .reception import require_reception, settle_due
from .schedule_policy import BEIJING, day_start, phase_at
from .schedules import schedule_snapshot
from .work_access import require_task
from .work_revisits import claim_due, ready_task, settle_window


def execution_busy(connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM runs WHERE status IN ('queued','running','stopping') "
            "UNION ALL SELECT 1 FROM execution_windows WHERE state!='closed' "
            "UNION ALL SELECT 1 FROM work_main_creations WHERE state!='closed' LIMIT 1"
        ).fetchone()
        is not None
    )


def working(connection, now):
    policy, state = schedule_snapshot(connection)
    return (
        not state["paused"] and policy is not None and phase_at(policy, state, now)[0] == "working"
    )


def window_row(connection, window_id):
    row = connection.execute("SELECT * FROM execution_windows WHERE id=?", (window_id,)).fetchone()
    if row is None:
        raise ApiError(404, "window_not_found", "没有找到工作窗口。")
    return row


def mark_stopping(connection, window, reason, now):
    connection.execute(
        "UPDATE execution_windows SET state=CASE WHEN state='unknown' THEN 'unknown' "
        "ELSE 'stopping' END,stop_reason=coalesce(stop_reason,?) WHERE id=? AND state!='closed'",
        (reason, window["id"]),
    )
    connection.execute(
        "UPDATE work_tasks SET status=CASE WHEN status='needs_reconciliation' THEN status "
        "ELSE 'stopping' END,stop_requested_at=coalesce(stop_requested_at,?),updated_at=? "
        "WHERE id=? AND status IN ('queued','running','stopping','needs_reconciliation')",
        (now, now, window["task_id"]),
    )


class ExecutionControl:
    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock

    def reserve(self, owner: str, task_id: str, seconds: int, *, decisions=False) -> dict:
        if type(seconds) is not int or not 1 <= seconds <= 3600:
            raise ValueError("Execution window must be 1–3600 seconds")
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            task = require_task(connection, owner, task_id)
            revisit = ready_task(connection, task, now, decisions=decisions)
            if now < task["created_at"]:
                raise ApiError(409, "execution_clock_rewind", "时钟异常，请先核对工作记录。")
            if execution_busy(connection):
                raise ApiError(409, "run_busy", "已有执行占用，请先等待结束或核对停止状态。")
            if not working(connection, now):
                raise ApiError(409, "work_schedule_closed", "当前只收件，尚未到工作时间。")
            settle_due(connection, now, owner=owner)
            require_reception(connection, owner, "work", now)
            seconds = reserve_budget(connection, now, seconds, required=True)
            day = datetime.fromtimestamp(now, BEIJING).date()
            deadline = min(now + seconds, day_start(day + timedelta(days=1)))
            window_id = str(uuid4())
            connection.execute(
                "INSERT INTO execution_windows VALUES (?,?,?,?,?,'held',NULL,NULL)",
                (window_id, task_id, "milo-work-" + uuid4().hex + ".service", now, deadline),
            )
            if revisit is not None:
                claim_due(connection, task, revisit, window_id, now)
            return dict(window_row(connection, window_id))

    def checkpoint(self, window_id: str) -> dict:
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            window = window_row(connection, window_id)
            if window["state"] in {"closed", "unknown", "stopping"}:
                return dict(window)
            task = connection.execute(
                "SELECT * FROM work_tasks WHERE id=?", (window["task_id"],)
            ).fetchone()
            reason = None
            try:
                require_task(connection, task["owner_id"], task["id"])
            except ApiError:
                reason = "access_changed"
            if reason is None:
                if task["stop_requested_at"] is not None or task["status"] not in {
                    "queued",
                    "running",
                }:
                    reason = "cancelled"
                elif now < window["created_at"]:
                    reason = "clock_rewind"
                elif now >= window["deadline_at"]:
                    reason = "timed_out"
                elif not held_budget_allows(connection, now):
                    reason = "cancelled"
                elif not working(connection, now):
                    reason = "schedule_closed"
            if reason:
                mark_stopping(connection, window, reason, now)
            return dict(window_row(connection, window_id))

    def close(self, window_id: str, *, stop: Callable, delivery_known: bool, outcome: str) -> bool:
        if type(delivery_known) is not bool or outcome not in {
            "completed",
            "failed",
            "cancelled",
            "timed_out",
        }:
            raise ValueError("Explicit delivery knowledge and outcome are required")
        with self.database.connect() as connection:
            if window_row(connection, window_id)["state"] == "closed":
                return True
        self.checkpoint(window_id)
        try:
            confirmed = stop() is True
        except Exception:
            confirmed = False
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            window = window_row(connection, window_id)
            if window["state"] == "closed":
                return True
            unknown_turn = connection.execute(
                "SELECT 1 FROM execution_permits WHERE window_id=? AND state='unknown'",
                (window_id,),
            ).fetchone()
            if not confirmed or not delivery_known or window["state"] == "unknown" or unknown_turn:
                connection.execute(
                    "UPDATE execution_permits SET state='unknown' "
                    "WHERE window_id=? AND state='launched'",
                    (window_id,),
                )
                connection.execute(
                    "UPDATE execution_windows SET state='unknown' WHERE id=?", (window_id,)
                )
                connection.execute(
                    "UPDATE work_tasks SET status='needs_reconciliation',updated_at=? WHERE id=?",
                    (now, window["task_id"]),
                )
                settle_window(connection, window_id, now, unknown=True)
                return False
            task = connection.execute(
                "SELECT * FROM work_tasks WHERE id=?", (window["task_id"],)
            ).fetchone()
            # Recheck changes made while the process family was being stopped.
            try:
                require_task(connection, task["owner_id"], task["id"])
            except ApiError:
                mark_stopping(connection, window, "access_changed", now)
            if not working(connection, now):
                mark_stopping(connection, window, "schedule_closed", now)
            if not held_budget_allows(connection, now):
                mark_stopping(connection, window, "cancelled", now)
            window = window_row(connection, window_id)
            latest = latest_execution_time(connection)
            if now < window["created_at"] or (latest is not None and now < latest):
                mark_stopping(connection, window, "clock_rewind", now)
                return False
            final = outcome
            if now >= window["deadline_at"] or window["stop_reason"] == "timed_out":
                final = "timed_out"
            elif (
                window["stop_reason"]
                or task["stop_requested_at"] is not None
                or task["status"] == "cancelled"
            ):
                final = "cancelled"
            connection.execute(
                "UPDATE execution_windows SET state='closed',ended_at=? WHERE id=?",
                (now, window_id),
            )
            connection.execute(
                "UPDATE execution_permits SET state=CASE WHEN state='reserved' "
                "THEN 'cancelled' ELSE 'finished' END,ended_at=? "
                "WHERE window_id=? AND state IN ('reserved','launched')",
                (now, window_id),
            )
            connection.execute(
                "UPDATE work_tasks SET status=?,updated_at=? WHERE id=?",
                (final, now, window["task_id"]),
            )
            settle_window(connection, window_id, now, outcome=final)
            return True

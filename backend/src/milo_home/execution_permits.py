"""Durable per-input turn permits; consuming twice never authorizes a resend."""

import time
from uuid import uuid4

from .errors import ApiError
from .execution_budget import held_budget_allows, shared_budget_view, shared_policy
from .execution_control import mark_stopping, window_row, working
from .work_access import require_task
from .work_reception import freeze_turn
from .work_revisits import settle_window


def active_window(connection, owner, window_id, now):
    window = window_row(connection, window_id)
    task = require_task(connection, owner, window["task_id"])
    if (
        window["state"] not in {"held", "running"}
        or window["stop_reason"] is not None
        or task["stop_requested_at"] is not None
        or task["status"] not in {"queued", "running"}
        or not window["created_at"] <= now < window["deadline_at"]
        or not working(connection, now)
        or shared_policy(connection) is None
        or not held_budget_allows(connection, now)
    ):
        raise ApiError(409, "execution_window_closed", "当前窗口不能继续调用。")
    return window


def permit_row(connection, permit_id):
    row = connection.execute("SELECT * FROM execution_permits WHERE id=?", (permit_id,)).fetchone()
    if row is None:
        raise ApiError(404, "permit_not_found", "没有找到本轮执行许可。")
    return row


class ExecutionPermits:
    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock

    def reserve(self, owner: str, window_id: str, input_id: str) -> dict:
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            window = active_window(connection, owner, window_id, now)
            input_ = connection.execute(
                "SELECT * FROM work_inputs WHERE id=? AND task_id=?", (input_id, window["task_id"])
            ).fetchone()
            if input_ is None:
                raise ApiError(404, "work_not_found", "没有找到可访问的工作输入。")
            previous = connection.execute(
                "SELECT * FROM execution_permits WHERE input_id=?", (input_id,)
            ).fetchone()
            if previous is not None:
                if previous["window_id"] != window_id:
                    raise ApiError(409, "permit_scope_changed", "该输入已有另一窗口的许可。")
                return dict(previous)
            if input_["delivery"] != "queued":
                raise ApiError(409, "work_input_conflict", "输入状态已变化，不能重新调用。")
            view = shared_budget_view(connection, shared_policy(connection), now, 1)
            remaining = view["remaining_launches"]
            if remaining is not None and remaining <= 0:
                raise ApiError(409, "daily_budget_exhausted", "本日调用次数已用完。")
            if connection.execute(
                "SELECT 1 FROM execution_permits WHERE window_id=? "
                "AND state IN ('reserved','launched','unknown')",
                (window_id,),
            ).fetchone():
                raise ApiError(409, "turn_busy", "本窗口还有未结束的一轮。")
            permit_id = str(uuid4())
            connection.execute(
                "INSERT INTO execution_permits VALUES (?,?,?,?,'reserved',NULL,NULL)",
                (permit_id, window_id, input_id, now),
            )
            return dict(permit_row(connection, permit_id))

    def consume(
        self,
        owner: str,
        permit_id: str,
        input_id: str,
        *,
        claim_input=False,
        freeze_reception=False,
    ) -> bool:
        """Call immediately before one turn/start, under the future coordinator lock."""
        now = int(self.clock())
        if type(claim_input) is not bool or type(freeze_reception) is not bool:
            raise ValueError("Explicit input claim required")
        with self.database.connect(write=True) as connection:
            permit = permit_row(connection, permit_id)
            window = window_row(connection, permit["window_id"])
            require_task(connection, owner, window["task_id"])
            if permit["input_id"] != input_id or permit["state"] != "reserved":
                return False
            try:
                active_window(connection, owner, window["id"], now)
            except ApiError:
                connection.execute(
                    "UPDATE execution_permits SET state='cancelled',ended_at=? WHERE id=?",
                    (now, permit_id),
                )
                mark_stopping(connection, window, "cancelled", now)
                return False
            if claim_input:
                input_ = connection.execute(
                    "SELECT * FROM work_inputs WHERE id=? AND task_id=?",
                    (input_id, window["task_id"]),
                ).fetchone()
                if input_ is None or input_["delivery"] != "queued":
                    return False
                if connection.execute(
                    "SELECT 1 FROM work_inputs WHERE task_id=? AND sequence<? "
                    "AND delivery!='acknowledged'",
                    (window["task_id"], input_["sequence"]),
                ).fetchone():
                    return False
                connection.execute(
                    "UPDATE work_inputs SET delivery='sending' WHERE id=?", (input_id,)
                )
                connection.execute(
                    "UPDATE work_tasks SET status='running',updated_at=? WHERE id=?",
                    (now, window["task_id"]),
                )
                connection.execute(
                    "UPDATE execution_windows SET state='running' WHERE id=?", (window["id"],)
                )
            if freeze_reception:
                freeze_turn(connection, permit_id, owner)
            connection.execute(
                "UPDATE execution_permits SET state='launched',launched_at=? WHERE id=?",
                (now, permit_id),
            )
            return True

    def cancel(self, permit_id: str) -> bool:
        with self.database.connect(write=True) as connection:
            permit = permit_row(connection, permit_id)
            if permit["state"] != "reserved":
                return False
            connection.execute(
                "UPDATE execution_permits SET state='cancelled',ended_at=? WHERE id=?",
                (int(self.clock()), permit_id),
            )
            return True

    def finish(self, permit_id: str, *, delivery_known: bool) -> bool:
        if type(delivery_known) is not bool:
            raise ValueError("Explicit delivery knowledge required")
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            permit = permit_row(connection, permit_id)
            if permit["state"] == "finished":
                return True
            if permit["state"] != "launched":
                return False
            if now < permit["launched_at"]:
                delivery_known = False
            connection.execute(
                "UPDATE execution_permits SET state=?,ended_at=? WHERE id=?",
                (
                    "finished" if delivery_known else "unknown",
                    now if delivery_known else None,
                    permit_id,
                ),
            )
            if not delivery_known:
                connection.execute(
                    "UPDATE execution_windows SET state='unknown' WHERE id=? AND state!='closed'",
                    (permit["window_id"],),
                )
                connection.execute(
                    "UPDATE work_tasks SET status='needs_reconciliation',updated_at=? "
                    "WHERE id=(SELECT task_id FROM execution_windows WHERE id=?)",
                    (now, permit["window_id"]),
                )
                settle_window(connection, permit["window_id"], now, unknown=True)
            return delivery_known

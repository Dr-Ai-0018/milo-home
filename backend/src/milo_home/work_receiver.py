"""Finite synchronous queue handoff; HTTP never starts a privileged process."""

import time
from contextlib import ExitStack
from dataclasses import dataclass

from .automatic_queue import automatic_choice
from .codex_rpc import ProtocolError
from .errors import ApiError
from .execution_budget import reserve_budget
from .execution_control import execution_busy, working
from .execution_lock import execution_lock
from .management_lease import current_boot
from .reception import require_reception
from .work_access import require_access, require_session
from .work_queue import decisions_enabled, next_work, observe_due
from .work_recovery import WorkRecovery


class ReceiveExpired(ProtocolError):
    pass


class ReceiveLimit:
    """One original wall and monotonic deadline, including idle and preparation."""

    def __init__(self, seconds, clock, monotonic=time.monotonic):
        if type(seconds) is not int or not 1 <= seconds <= 3600:
            raise ValueError("Receive timeout must be 1–3600 seconds")
        self.clock = clock
        self.monotonic = monotonic
        self.last_wall = clock()
        self.wall_deadline = self.last_wall + seconds
        self.monotonic_deadline = monotonic() + seconds

    def remaining(self):
        now = self.clock()
        if now < self.last_wall:
            raise ProtocolError("Receive clock moved backwards")
        self.last_wall = now
        remaining = min(self.wall_deadline - now, self.monotonic_deadline - self.monotonic())
        if remaining < 1:
            raise ReceiveExpired("Receive window expired")
        return remaining

    def seconds(self):
        return int(self.remaining())


@dataclass(frozen=True)
class ReceiveResult:
    outcome: str
    task_id: str | None = None
    window_id: str | None = None
    turns: int = 0


def receive_context(connection, owner, session_id, original, now, seconds):
    """Read current authority, shared occupancy, schedule and budget in one snapshot."""
    session = require_session(connection, owner, session_id)
    access = require_access(connection, owner, session["workspace_id"])
    if (access["owner_revision"], access["workspace_revision"]) != original[1:]:
        raise ProtocolError("Receive authorization changed during selection")
    if execution_busy(connection):
        raise ApiError(409, "run_busy", "已有执行占用，请先核对停止状态。")
    if not working(connection, now):
        raise ApiError(409, "work_schedule_closed", "当前只收件，尚未到工作时间。")
    require_reception(connection, owner, "work", now)
    allowed = reserve_budget(connection, now, seconds, required=True)
    return access, allowed


class WorkReceiver:
    def __init__(self, launcher, stop, limit: ReceiveLimit, *, interval=2):
        if type(interval) is not int or not 1 <= interval <= 30:
            raise ValueError("Receive interval must be 1–30 seconds")
        self.launcher = launcher
        self.runtime = launcher.runtime
        self.stop = stop
        self.limit = limit
        self.interval = interval

    def run(self, owner, session_id, *, credential):
        original = None
        while not self.stop.is_set():
            try:
                with execution_lock(self.runtime.database) as lease:
                    self.limit.remaining()
                    identity = self.runtime.check_session_binding(owner, session_id, lease=lease)
                    if original is None:
                        original = identity
                    elif identity != original:
                        raise ProtocolError("Receive association or authorization changed")
                    task_id = self._next_task(owner, session_id, lease, original)
                    if task_id is not None:
                        window_id, result = self.launcher._run_task_locked(
                            owner,
                            task_id,
                            self.limit.seconds(),
                            credential=credential,
                            lease=lease,
                            limit=self.limit,
                        )
                        break
                self.stop.wait(min(self.interval, self.limit.remaining()))
            except ReceiveExpired:
                return ReceiveResult("wait_expired")
        else:
            return ReceiveResult("stop_requested")
        # Reacquire the same public lock through recovery, as work run does.
        WorkRecovery(self.runtime).replay_results(owner, window_id)
        return ReceiveResult(result.outcome, task_id, window_id, result.turns)

    def _next_task(self, owner, session_id, lease, original):
        self.runtime._identity(lease)
        if self.runtime.journal.outstanding():
            raise ProtocolError("Previous private execution still needs reconciliation")
        with self.runtime.database.connect() as connection:
            connection.execute("BEGIN")
            now = int(self.runtime.clock())
            access, _ = receive_context(
                connection, owner, session_id, original, now, self.limit.seconds()
            )
            task_id = first_queued(
                connection,
                owner,
                session_id,
                access,
                now=now,
                decisions=self.runtime.native_policy is not None,
            )
        self.runtime._identity(lease)
        return task_id


def first_queued(connection, owner, session_id, access, *, now=0, decisions=False):
    row = next_work(connection, owner, session_id, access, now, decisions=decisions)
    return row["id"] if row else None


def receive_once(launcher, stop, owner, session_id, seconds, *, credential):
    """Do not wait, reserve idle time, or pick another task after any outcome."""
    runtime = launcher.runtime
    with ExitStack() as stack:
        try:
            lease = stack.enter_context(execution_lock(runtime.database))
        except ApiError as error:
            if error.code != "worker_busy":
                raise
            return ReceiveResult(error.code)
        if stop.is_set():
            return ReceiveResult("stop_requested")
        original = runtime.check_session_binding(owner, session_id, lease=lease)
        with runtime.database.connect(write=True) as connection:
            now = int(runtime.clock())
            enabled = decisions_enabled(connection, owner)
            if enabled and runtime.native_policy is None:
                raise ProtocolError("Registered work decisions require the original native main")
            observe_due(connection, owner, now, current_boot())
            try:
                access, _ = receive_context(connection, owner, session_id, original, now, seconds)
            except ApiError as error:
                if error.code not in {
                    "run_busy",
                    "work_schedule_closed",
                    "daily_budget_exhausted",
                    "reception_closed",
                    "reception_clock_invalid",
                }:
                    raise
                return ReceiveResult(error.code)
            task_id = first_queued(
                connection, owner, session_id, access, now=now, decisions=enabled
            )
            choice = automatic_choice(
                connection, "work", owner, now, current_boot(), session=session_id
            )
            if task_id is not None and (choice is None or choice["kind"] != "work"):
                return ReceiveResult("other_entry_first")
        runtime._identity(lease)
        if task_id is None:
            return ReceiveResult("queue_empty")
        # The launcher rechecks the binding and schedule, then starts the
        # work window at reservation time, after the one-time selection.
        try:
            window_id, result = launcher._run_task_locked(
                owner, task_id, seconds, credential=credential, lease=lease
            )
        except ApiError as error:
            # These errors arise before the atomic window reservation. There is
            # no accepted work window or possible delivery to recover here.
            if error.code not in {"reception_closed", "reception_clock_invalid"}:
                raise
            return ReceiveResult(error.code)
    WorkRecovery(runtime).replay_results(owner, window_id)
    return ReceiveResult(result.outcome, task_id, window_id, result.turns)

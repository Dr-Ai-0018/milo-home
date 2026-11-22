"""Bounded synchronous coordination; no CLI, service, or production enablement."""

from dataclasses import dataclass
from typing import Protocol

from .errors import ApiError
from .worker import Worker


class StopControl(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, seconds: float) -> bool: ...


@dataclass(frozen=True)
class SchedulerResult:
    cycles: int
    succeeded: int
    idle: int
    busy: int
    reason: str


class Scheduler:
    def __init__(self, worker: Worker, *, interval: int = 15, timeout: int = 3600):
        if type(interval) is not int or not 1 <= interval <= 300:
            raise ValueError("Scan interval must be an integer from 1 to 300 seconds")
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError("Run timeout must be an integer from 1 to 3600 seconds")
        self.worker = worker
        self.interval = interval
        self.timeout = timeout

    def run(self, *, cycles: int, stop: StopControl) -> SchedulerResult:
        """Wait after each completed cycle, without parallel workers or catch-up bursts.

        The caller owns signal handling and stopping an in-flight Worker. The stop
        event interrupts idle waits and prevents the next cycle; it does not claim
        that a running subprocess was stopped. Exceptions propagate to the caller.
        """
        if type(cycles) is not int or cycles < 1:
            raise ValueError("An explicit positive cycle count is required")
        completed = succeeded = idle = busy = 0
        reason = "cycles_exhausted"
        for _ in range(cycles):
            if stop.is_set():
                reason = "stop_requested"
                break
            try:
                run = self.worker.wake_automatic(self.timeout)
            except ApiError as error:
                if error.code in {"daily_budget_exhausted", "budget_clock_rewind"}:
                    return SchedulerResult(completed + 1, succeeded, idle, busy, error.code)
                if error.code != "worker_busy":
                    raise
                busy += 1
            else:
                if run is None:
                    idle += 1
                elif run["status"] == "succeeded":
                    succeeded += 1
                else:
                    # Even a confirmed failure must not silently start a new batch.
                    # Recovery and any allowed reservation retry remain explicit.
                    return SchedulerResult(
                        completed + 1, succeeded, idle, busy, "run_" + run["status"]
                    )
            completed += 1
            if stop.is_set():
                reason = "stop_requested"
                break
            if completed < cycles and stop.wait(self.interval):
                reason = "stop_requested"
                break
        return SchedulerResult(completed, succeeded, idle, busy, reason)

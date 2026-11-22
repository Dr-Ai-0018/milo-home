"""Renew management evidence synchronously; never switch identities in a thread."""

import time

from .execution_pulse import PULSE_INTERVAL


class ManagedStop:
    def __init__(self, stop, lease, *, monotonic=time.monotonic):
        self.stop = stop
        self.lease = lease
        self.monotonic = monotonic

    def progress(self):
        if not self.stop.is_set() and not self.lease.progress():
            self.stop.set()

    def is_set(self) -> bool:
        self.progress()
        return self.stop.is_set()

    def wait(self, seconds: float) -> bool:
        deadline = self.monotonic() + seconds
        while not self.is_set():
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            if self.stop.wait(min(remaining, PULSE_INTERVAL)):
                break
        return self.stop.is_set()

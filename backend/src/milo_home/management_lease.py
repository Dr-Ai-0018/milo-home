"""Short-lived management evidence, separate from every execution permission."""

import secrets
import time
from pathlib import Path

from .automatic_budget import AutomaticPolicy
from .errors import ApiError
from .execution_pulse import PULSE_INTERVAL, PULSE_TTL, identity_hash


def current_boot() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def observe_management(row, now: int, boot: str) -> tuple[str, str, int | None]:
    if row is None:
        return "unknown", "management_not_configured", None
    if (
        row["boot_id"] != boot
        or row["invalid_reason"] == "clock_invalid"
        or not row["started_at"] <= row["heartbeat_at"] <= now
        or (row["ended_at"] is not None and not row["heartbeat_at"] <= row["ended_at"] <= now)
    ):
        return "unknown", "management_clock_or_boot_invalid", None
    until = (row["ended_at"] or row["heartbeat_at"]) + PULSE_TTL
    if row["invalid_reason"] or now >= until:
        return "unknown", "management_lease_expired", None
    if row["ended_at"] is not None:
        return "stopped", "management_stopped", until
    return "enabled", "management_lease_current", until


class ManagementLease:
    def __init__(
        self,
        database,
        policy: AutomaticPolicy,
        *,
        clock=time.time,
        monotonic=time.monotonic,
        boot: str | None = None,
    ):
        self.database = database
        self.clock = clock
        self.monotonic = monotonic
        self.boot = current_boot() if boot is None else boot
        self.policy_hash = identity_hash(policy.model_dump_json())
        self.identity = secrets.token_urlsafe(32)
        self.next_pulse = 0.0
        self.lost = False
        self.claimed = False

    def claim(self):
        if self.claimed:
            raise ValueError("A management identity may only claim once")
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            previous = connection.execute("SELECT * FROM management_lease WHERE id = 1").fetchone()
            if previous is not None and previous["boot_id"] == self.boot:
                if now < max(previous["heartbeat_at"], previous["ended_at"] or 0) or (
                    previous["ended_at"] is None and now < previous["heartbeat_at"] + PULSE_TTL
                ):
                    raise ApiError(409, "management_busy", "自动管理仍有有效占用或时钟异常。")
            connection.execute(
                "INSERT OR REPLACE INTO management_lease "
                "(id, identity_hash, boot_id, policy_hash, started_at, heartbeat_at) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (identity_hash(self.identity), self.boot, self.policy_hash, now, now),
            )
        self.claimed = True
        self.next_pulse = self.monotonic() + PULSE_INTERVAL

    def renew(self) -> bool:
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM management_lease WHERE id = 1 AND identity_hash = ?",
                (identity_hash(self.identity),),
            ).fetchone()
            if row is None or row["ended_at"] is not None:
                return False
            state, reason, _ = observe_management(row, now, self.boot)
            if state != "enabled":
                invalid = "clock_invalid" if reason.endswith("invalid") else "lease_expired"
                connection.execute(
                    "UPDATE management_lease SET invalid_reason = ? WHERE id = 1", (invalid,)
                )
                return False
            connection.execute("UPDATE management_lease SET heartbeat_at = ? WHERE id = 1", (now,))
        return True

    def progress(self) -> bool:
        if self.lost:
            return False
        if self.monotonic() >= self.next_pulse:
            self.lost = not self.renew()
            self.next_pulse = self.monotonic() + PULSE_INTERVAL
        return not self.lost

    def close(self):
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM management_lease WHERE id = 1 AND identity_hash = ?",
                (identity_hash(self.identity),),
            ).fetchone()
            if row is None or row["ended_at"] is not None:
                return
            state, reason, _ = observe_management(row, now, self.boot)
            if state != "enabled":
                invalid = "clock_invalid" if reason.endswith("invalid") else "lease_expired"
                connection.execute(
                    "UPDATE management_lease SET invalid_reason = ? WHERE id = 1", (invalid,)
                )
                return
            connection.execute(
                "UPDATE management_lease SET ended_at = ? WHERE id = 1 AND identity_hash = ? "
                "AND ended_at IS NULL",
                (now, identity_hash(self.identity)),
            )

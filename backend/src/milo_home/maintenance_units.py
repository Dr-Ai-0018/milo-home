"""Small, durable and injectable systemd pause/restore boundary for one capture."""

import errno
import hashlib
import json
import os
import re
import socket
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from .database_identity import private_run_directory
from .maintenance_policy import trusted_bytes
from .workspace_archive_files import json_bytes, write_file

PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "Result",
    "ExecMainStatus",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
)


class MaintenanceBusyError(RuntimeError):
    """Another maintenance command owns this exact project entry set."""


def systemd_unit_runner(action, unit):
    if action not in {"show", "start", "stop"} or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,200}\.(service|timer)", unit
    ):
        raise ValueError("Unsupported maintenance unit operation")
    command = ["systemctl", "--no-pager", action, unit]
    if action == "show":
        command.extend("--property=" + name for name in PROPERTIES)
    result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
    if result.returncode or len(result.stdout) > 16384:
        raise RuntimeError("Maintenance unit operation failed; private details suppressed")
    if action == "show":
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return None


@dataclass(frozen=True)
class MaintenanceUnits:
    intake_timers: tuple[str, ...] = ("milo-home-work.timer", "milo-home-reply.timer")
    workers: tuple[str, ...] = ("milo-home-work.service", "milo-home-reply.service")
    api: str = "milo-home-api.service"

    def __post_init__(self):
        timers = self.intake_timers
        services = (*self.workers, self.api)
        if not isinstance(timers, tuple) or not isinstance(self.workers, tuple):
            raise ValueError("Maintenance units require explicit tuples")
        all_units = (*timers, *services)
        if len(all_units) > 16 or len(set(all_units)) != len(all_units):
            raise ValueError("Maintenance units overlap or exceed the finite bound")
        for names, suffix in ((timers, "timer"), (services, "service")):
            if any(not re.fullmatch(r"milo-home-[a-z0-9-]+\." + suffix, name) for name in names):
                raise ValueError("Maintenance can only pause explicitly named project entries")


@contextmanager
def maintenance_lock(units):
    """Avoid two maintenance commands restoring each other's intake state."""
    names = sorted((*units.intake_timers, *units.workers, units.api))
    address = b"\0milo-home-maintenance-" + hashlib.sha256(json_bytes(names)).hexdigest().encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as marker:
        marker.set_inheritable(False)
        try:
            marker.bind(address)
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            raise MaintenanceBusyError("Maintenance coordination is occupied") from error
        yield


def _known(values, *, idle=False, timer=False):
    if values.get("LoadState") != "loaded":
        raise ValueError("Maintenance unit state is unknown")
    state = values.get("ActiveState")
    if state not in {"active", "inactive", "failed"}:
        raise ValueError("Maintenance unit is transitioning or unknown")
    if idle and (
        state not in {"inactive", "failed"} or (not timer and values.get("MainPID") != "0")
    ):
        raise ValueError("Maintenance unit is not confirmed idle")
    return state


class MaintenanceSession:
    def __init__(self, units, runner, state_file, *, clock):
        self.units = units
        self.runner = runner
        self.path = state_file
        private_run_directory(self.path.parent)
        if self.path.resolve() != self.path or self.path.exists() or self.path.is_symlink():
            raise ValueError("Maintenance state requires a new canonical private file")
        original = {}
        for unit in (*units.intake_timers, *units.workers, units.api):
            original[unit] = _known(runner("show", unit))
        self.state = {
            "version": 1,
            "timestamp": int(clock()),
            "original": original,
            "paused": [],
            "restored": [],
            "restore_pending": [],
            "phase": "prepared",
        }
        write_file(self.path, json_bytes(self.state))
        self.save()

    def save(self):
        temporary = self.path.with_name("." + self.path.name + "." + uuid4().hex)
        write_file(temporary, json_bytes(self.state))
        os.replace(temporary, self.path)
        descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def pause(self, unit):
        if self.state["original"][unit] != "active":
            return
        # Record the stop intention before the command, including ambiguous failures.
        self.state["paused"].append(unit)
        self.state["phase"] = "pausing"
        self.save()
        self.runner("stop", unit)
        _known(self.runner("show", unit), idle=True, timer=unit.endswith(".timer"))

    def workers_idle(self):
        for unit in self.units.workers:
            _known(self.runner("show", unit), idle=True)

    def restore(self):
        self.state["restore_pending"] = []
        ordered = [self.units.api, *self.units.intake_timers]
        for unit in ordered:
            if unit not in self.state["paused"] or unit in self.state["restored"]:
                continue
            if self.state["restore_pending"]:
                self.state["restore_pending"].append(unit)
                continue
            try:
                current = self.runner("show", unit)
                state = _known(current)
                if state != "active":
                    _known(current, idle=True, timer=unit.endswith(".timer"))
                    self.runner("start", unit)
                if _known(self.runner("show", unit)) != "active":
                    raise ValueError("Maintenance restoration was not confirmed")
                self.state["restored"].append(unit)
            except Exception:
                self.state["restore_pending"].append(unit)
            self.save()
        self.state["phase"] = "restore_pending" if self.state["restore_pending"] else "restored"
        self.save()
        return {
            "restored": list(self.state["restored"]),
            "restore_pending": list(self.state["restore_pending"]),
        }


def restore_maintenance_entries(state_file, *, units, unit_runner):
    """Explicit recovery of recorded entries; never repeat capture or remote work."""
    state = json.loads(trusted_bytes(state_file, limit=64 * 1024))
    expected = set((*units.intake_timers, *units.workers, units.api))
    mutable = set((*units.intake_timers, units.api))
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or not isinstance(state.get("original"), dict)
        or set(state["original"]) != expected
        or any(
            value not in {"active", "inactive", "failed"} for value in state["original"].values()
        )
        or any(not isinstance(state.get(name), list) for name in ("paused", "restored"))
        or len(state["paused"]) != len(set(state["paused"]))
        or not set(state["paused"]) <= mutable
        or not set(state["restored"]) <= set(state["paused"])
        or any(state["original"][unit] != "active" for unit in state["paused"])
    ):
        raise ValueError("Saved maintenance state does not match the explicit project entries")
    with maintenance_lock(units):
        session = object.__new__(MaintenanceSession)
        session.units = units
        session.runner = unit_runner
        session.path = state_file
        session.state = state
        return session.restore()

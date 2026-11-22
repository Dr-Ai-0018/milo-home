"""Fresh, bounded stopping of a recorded host family; never launches a process."""

import re
import subprocess
from pathlib import Path
from uuid import UUID

from .codex_process import native_unit_name
from .codex_rpc import ProtocolError


def boot_id():
    return str(UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))


def identity_shape(identity):
    try:
        unit = identity["unit"]
        group = identity["group"]
        valid = (
            set(identity) == {"unit", "group", "boot_id", "invocation", "main_pid"}
            and native_unit_name(unit)
            and re.fullmatch(r"[0-9a-f]{32}", identity["invocation"])
            and re.fullmatch(r"/[A-Za-z0-9_./:@\x2d]+", group)
            and ".." not in group.split("/")
            and Path(group).as_posix() == group
            and Path(group).name == unit
            and type(identity["main_pid"]) is int
            and identity["main_pid"] > 0
            and str(UUID(identity["boot_id"])) == identity["boot_id"]
        )
        if not valid:
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProtocolError("Missing exact historical process identity") from None


def stop_shape(event):
    try:
        identity = {k: v for k, v in event.items() if k not in {"kind", "checked_at"}}
        identity_shape(identity)
        if (
            set(event) != set(identity) | {"kind", "checked_at"}
            or event["kind"] != "work_stop"
            or type(event["checked_at"]) is not int
            or event["checked_at"] < 0
        ):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProtocolError("Invalid recovery stop evidence") from None


def recorded_identity(entries, unit):
    ready = [e for _, e in entries if e.get("kind") == "process" and e.get("phase") == "ready"]
    if len(ready) != 1 or ready[0].get("unit") != unit:
        raise ProtocolError("Missing exact historical process identity")
    identity = {
        key: ready[0].get(key) for key in ["unit", "group", "boot_id", "invocation", "main_pid"]
    }
    identity_shape(identity)
    return identity


def _show(run, unit):
    result = run(
        [
            "systemctl",
            "show",
            unit,
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
            "-p",
            "ControlGroup",
            "-p",
            "InvocationID",
            "-p",
            "MainPID",
        ],
        capture_output=True,
        text=True,
        timeout=3,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode and values.get("LoadState") != "not-found":
        return None
    return values


def _matches(values, identity):
    if values is None:
        return False
    if values.get("LoadState") == "not-found":
        return (
            values.get("ActiveState") == "inactive"
            and values.get("MainPID") == "0"
            and not values.get("InvocationID")
            and not values.get("ControlGroup")
        )
    return (
        values.get("LoadState") == "loaded"
        and values.get("InvocationID") == identity["invocation"]
        and values.get("ControlGroup") in {identity["group"], ""}
        and values.get("MainPID") in {str(identity["main_pid"]), "0"}
    )


def stop_family(identity, *, run=subprocess.run, cgroup_root=Path("/sys/fs/cgroup")):
    identity_shape(identity)
    try:
        if boot_id() != identity["boot_id"]:
            return False
        before = _show(run, identity["unit"])
        if not _matches(before, identity):
            return False
        if before["ActiveState"] not in {"inactive", "failed"}:
            if before.get("ControlGroup") != identity["group"] or before.get("MainPID") != str(
                identity["main_pid"]
            ):
                return False
            stopped = run(["systemctl", "stop", identity["unit"]], capture_output=True, timeout=6)
            if stopped.returncode:
                return False
        after = _show(run, identity["unit"])
        if (
            boot_id() != identity["boot_id"]
            or not _matches(after, identity)
            or after["ActiveState"] not in {"inactive", "failed"}
            or after.get("MainPID") != "0"
        ):
            return False
        group = cgroup_root / identity["group"].lstrip("/")
        if group.resolve() != group:
            return False
        if group.exists():
            events = dict(
                line.split() for line in (group / "cgroup.events").read_text().splitlines()
            )
            return events.get("populated") == "0"
        return after.get("LoadState") == "not-found"
    except (OSError, ValueError, subprocess.SubprocessError):
        return False

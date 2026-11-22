"""Bound development commands before they start; refuse competing host load."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4


def sample():
    fields = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
    return sum(fields), fields[3] + fields[4]


def busy(previous, current):
    total = current[0] - previous[0]
    return 100 * (1 - (current[1] - previous[1]) / total) if total else 0


def stop(process, unit):
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        pass
    subprocess.run(
        ["systemctl", "stop", unit], capture_output=True, timeout=8, check=False
    )
    process.wait(timeout=3)
    result = subprocess.run(
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
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    if values.get("LoadState") == "not-found":
        return
    group = values.get("ControlGroup", "")
    if values.get("ActiveState") not in {"inactive", "failed"}:
        raise RuntimeError("Development scope stop could not be confirmed")
    if group:
        if not group.startswith("/") or ".." in group.split("/"):
            raise RuntimeError("Invalid development cgroup evidence")
        path = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.events"
        if path.exists():
            events = dict(line.split() for line in path.read_text().splitlines())
            if events.get("populated") != "0":
                raise RuntimeError("Development descendants have not stopped")


def main():
    command = sys.argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SystemExit(
            "Usage: python3 scripts/guard-command.py COMMAND [ARGUMENT ...]"
        )
    available = sorted(os.sched_getaffinity(0))
    selected = available[: max(1, len(available) // 2)]
    cpu_set = ",".join(map(str, selected))
    previous = sample()
    time.sleep(1)
    current = sample()
    if busy(previous, current) >= 70:
        print("Stopped before command: host CPU is at least 70%", flush=True)
        return 80
    unit = "milo-validation-" + uuid4().hex + ".scope"
    process = subprocess.Popen(
        [
            "systemd-run",
            "--scope",
            "--quiet",
            "--unit=" + unit,
            "-p",
            "AllowedCPUs=" + cpu_set,
            "-p",
            "CPUQuota=50%",
            "-p",
            "CPUQuotaPeriodSec=10ms",
            "-p",
            "MemoryMax=768M",
            "-p",
            "RuntimeMaxSec=50s",
            "-p",
            "TimeoutStopSec=3s",
            *command,
        ],
        start_new_session=True,
    )
    print(
        "Development CPUs: " + cpu_set + "; per-command quota: half a CPU", flush=True
    )
    end = time.monotonic() + 50
    previous = current
    try:
        while process.poll() is None:
            time.sleep(0.5)
            current = sample()
            load = busy(previous, current)
            previous = current
            if load >= 80 or time.monotonic() >= end:
                print(
                    "Stopping command: host CPU/deadline guard",
                    round(load, 1),
                    flush=True,
                )
                return 80
    finally:
        stop(process, unit)
    return process.returncode


if __name__ == "__main__":
    sys.exit(main())

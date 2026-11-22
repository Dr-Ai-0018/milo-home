"""Own one finite host execution family; resource limits are not a file sandbox."""

import os
import re
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from .codex_rpc import ProtocolError


def native_unit_name(value):
    return (
        isinstance(value, str)
        and re.fullmatch(
            r"milo-(?:work-[0-9a-f]{32}|"
            r"reply-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.service",
            value,
        )
        is not None
    )


def cpu_set(value):
    """Parse the kernel's effective CPU list, rejecting empty or malformed evidence."""
    result = set()
    for part in value.strip().split(","):
        bounds = part.split("-")
        if not 1 <= len(bounds) <= 2 or any(not n.isdecimal() for n in bounds):
            raise ValueError("Invalid effective CPU list")
        first, last = int(bounds[0]), int(bounds[-1])
        if not first <= last < 65536:
            raise ValueError("Invalid effective CPU range")
        result.update(range(first, last + 1))
    return result


class DirectUnit:
    def __init__(self, *, seconds: int, run=subprocess.run, cgroup_root=Path("/sys/fs/cgroup")):
        if type(seconds) is not int or not 1 <= seconds <= 3600:
            raise ValueError("Execution window must be 1–3600 seconds")
        self.seconds = seconds
        self.allowed_cpus = frozenset(os.sched_getaffinity(0))
        self.deadline = time.monotonic() + seconds
        self.name = "milo-work-" + uuid4().hex + ".service"
        self.run = run
        self.cgroup_root = cgroup_root
        self.group = None
        self.process = None
        self.launch_attempted = False
        self.wall_deadline = None
        self.last_wall_time = None
        self.observed = False
        self.invocation = None
        self.main_pid = None
        self.previous_cpu = self._cpu_sample()
        self.next_cpu = time.monotonic() + 1

    @classmethod
    def from_reservation(cls, *, unit_name: str, created_at: int, deadline_at: int, **kwargs):
        """Use a persisted window; the caller still owns authorization and fencing."""
        if not native_unit_name(unit_name):
            raise ValueError("Invalid execution family identity")
        if (
            type(created_at) is not int
            or type(deadline_at) is not int
            or not 1 <= deadline_at - created_at <= 3600
        ):
            raise ValueError("Invalid persisted execution window")
        unit = cls(seconds=deadline_at - created_at, **kwargs)
        unit.name = unit_name
        unit.wall_deadline = deadline_at
        unit.last_wall_time = created_at
        # Pin remaining wall time to monotonic time so a later clock rollback
        # cannot grant another full window, including during launch preparation.
        unit.deadline = min(unit.deadline, time.monotonic() + unit._remaining())
        return unit

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if self.wall_deadline is not None:
            now = time.time()
            if now < self.last_wall_time:
                raise ProtocolError("Execution clock moved backwards")
            self.last_wall_time = now
            remaining = min(remaining, self.wall_deadline - now)
        if remaining <= 0:
            raise ProtocolError("Execution window expired")
        return remaining

    def launch(self, argv: list[str], *, environment_file: Path | None = None):
        if self.launch_attempted:
            raise ProtocolError("Execution window unavailable")
        if not argv or not all(isinstance(value, str) and "\x00" not in value for value in argv):
            raise ValueError("Invalid trusted command")
        properties = [
            "CPUQuota=50%",
            "CPUQuotaPeriodSec=10ms",
            "AllowedCPUs=" + ",".join(map(str, sorted(self.allowed_cpus))),
            "MemoryMax=768M",
            "TasksMax=64",
            "KillMode=control-group",
            "TimeoutStopSec=3s",
        ]
        if environment_file is not None:
            metadata = environment_file.stat()
            if (
                not environment_file.is_absolute()
                or environment_file.resolve() != environment_file
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
                or not environment_file.is_file()
            ):
                raise ValueError("Environment file must be private and owned by the caller")
            properties.append("EnvironmentFile=" + str(environment_file))
        seconds = int(self._remaining())
        if seconds < 1:
            raise ProtocolError("Execution window expired")
        properties.append(f"RuntimeMaxSec={seconds}s")
        command = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect", "--unit=" + self.name]
        for value in properties:
            command += ["-p", value]
        command += ["--", *argv]
        # A failed spawn does not prove that nothing started. Do not retry here.
        self.launch_attempted = True
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        return self.process

    def _properties(self) -> dict | None:
        try:
            result = self.run(
                [
                    "systemctl",
                    "show",
                    self.name,
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
        except (OSError, subprocess.SubprocessError):
            return None
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if result.returncode and properties.get("LoadState") != "not-found":
            return None
        invocation = properties.get("InvocationID", "")
        if self.invocation is not None and invocation and invocation != self.invocation:
            return None
        group = properties.get("ControlGroup", "")
        if group:
            if not re.fullmatch(r"/[A-Za-z0-9_./:@\x2d]+", group) or ".." in group.split("/"):
                return None
            path = self.cgroup_root / group.lstrip("/")
            if self.group is not None and path != self.group:
                return None
            self.group = path
        return properties

    def ready(self) -> bool:
        properties = self._properties()
        if not properties or properties.get("ActiveState") != "active" or self.group is None:
            return False
        try:
            invocation = properties.get("InvocationID", "")
            main_pid = int(properties.get("MainPID", "0"))
            members = (self.group / "cgroup.procs").read_text().split()
            quota, period = (self.group / "cpu.max").read_text().split()
            memory = (self.group / "memory.max").read_text().strip()
            tasks = (self.group / "pids.max").read_text().strip()
            effective = cpu_set((self.group / "cpuset.cpus.effective").read_text())
            valid = (
                re.fullmatch(r"[0-9a-f]{32}", invocation) is not None
                and main_pid > 0
                and str(main_pid) in members
                and (self.main_pid is None or main_pid == self.main_pid)
                and quota != "max"
                and 0 < int(period) <= 10000
                and 0 < int(quota) / int(period) <= 0.5
                and memory != "max"
                and 0 < int(memory) <= 768 * 1024 * 1024
                and tasks != "max"
                and 0 < int(tasks) <= 64
                and effective <= self.allowed_cpus
            )
        except (OSError, ValueError, ZeroDivisionError):
            return False
        self.observed = valid
        if valid:
            self.invocation = invocation
            self.main_pid = main_pid
        return valid

    def wait_ready(self):
        end = min(self.deadline, time.monotonic() + 3)
        while time.monotonic() < end:
            self.checkpoint()
            if self.ready():
                return
            if self.process is None or self.process.poll() is not None:
                break
            time.sleep(0.02)
        raise ProtocolError("Execution group not verified")

    def empty(self) -> bool:
        properties = self._properties()
        if not self.observed or properties is None:
            return False
        if self.process is not None and self.process.poll() is None:
            return False
        if properties.get("ActiveState") not in {"inactive", "failed"}:
            return False
        try:
            if self.group.exists():
                values = dict(
                    line.split() for line in (self.group / "cgroup.events").read_text().splitlines()
                )
                return values.get("populated") == "0"
            return properties.get("LoadState") == "not-found"
        except (OSError, ValueError):
            return False

    def stop(self) -> bool:
        if self.invocation is not None and self._properties() is None:
            return False
        try:
            self.run(["systemctl", "stop", self.name], capture_output=True, timeout=6)
            if self.process is not None:
                self.process.wait(timeout=4)
        except (OSError, subprocess.SubprocessError):
            return False
        return self.empty()

    def checkpoint(self):
        self._remaining()
        now = time.monotonic()
        if now >= self.next_cpu:
            current = self._cpu_sample()
            total = current[0] - self.previous_cpu[0]
            busy = 100 * (1 - (current[1] - self.previous_cpu[1]) / total) if total else 0
            self.previous_cpu = current
            self.next_cpu = now + 1
            if busy > 80:
                raise ProtocolError("Host CPU limit")

    @staticmethod
    def _cpu_sample():
        fields = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
        return sum(fields), fields[3] + fields[4]

import json
import os
import selectors
import subprocess
import time
from pathlib import Path

from .execution_pulse import PULSE_INTERVAL

DISABLED_CODE_MODE_NOTICE = (
    "Code Mode is unavailable because code-mode host is disabled. "
    "Code mode will fail closed; enable `features.code_mode_host` "
    "and install `codex-code-mode-host`."
)
LEGACY_MULTI_AGENT_NOTICE = (
    "`[features].multi_agent` is deprecated. It still controls the V1 fallback. "
    "`[agents].enabled = false` disables subagent tools even when the model selects "
    "a subagent version, unless `[features].multi_agent_v2` is enabled."
)
DISABLED_CAPABILITY_NOTICES = {DISABLED_CODE_MODE_NOTICE, LEGACY_MULTI_AGENT_NOTICE}


def cpu_sample():
    values = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
    return sum(values), values[3] + values[4]


def unit_empty(unit: str) -> bool:
    try:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                unit,
                "-p",
                "ControlGroup",
                "-p",
                "ActiveState",
                "-p",
                "LoadState",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if properties.get("LoadState") == "not-found" and properties.get("ActiveState") == "inactive":
        return True
    if result.returncode:
        return False
    group = properties.get("ControlGroup", "")
    if group:
        directory = Path("/sys/fs/cgroup") / group.lstrip("/")
        if directory.exists() and any(
            path.read_text().strip() for path in directory.rglob("cgroup.procs")
        ):
            return False
    return properties.get("ActiveState") in {"inactive", "failed"}


def stop_unit(unit: str) -> bool:
    try:
        subprocess.run(
            ["systemctl", "stop", unit],
            capture_output=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return unit_empty(unit)


class EventStream:
    def __init__(self):
        self.completed = 0
        self.failed = False
        self.invalid = False
        self.buffer = b""
        self.size = 0

    def feed(self, data: bytes, *, stdout: bool):
        self.size += len(data)
        if self.size > 2 * 1024 * 1024:
            self.invalid = True
            return
        if not stdout:
            return
        self.buffer += data
        if len(self.buffer) > 256 * 1024:
            self.invalid = True
            return
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                event = json.loads(line)
                kind = event["type"]
                if not isinstance(kind, str):
                    raise ValueError("Invalid event")
                if kind == "turn.completed":
                    self.completed += 1
                if kind in {"turn.failed", "error"}:
                    self.failed = True
                item = event.get("item", {})
                if item.get("type") == "error":
                    benign_notice = (
                        kind == "item.completed"
                        and item.get("message") in DISABLED_CAPABILITY_NOTICES
                    )
                    if not benign_notice:
                        self.failed = True
                elif item and item.get("type") not in {"agent_message", "reasoning"}:
                    self.invalid = True
            except (ValueError, KeyError, TypeError, AttributeError):
                self.invalid = True


def execute(command: list[str], prompt: Path, store, run_id: str) -> tuple[int, EventStream, bool]:
    run = store.checkpoint(run_id)
    unit = run["unit_name"]
    events = EventStream()
    if run["stop_reason"]:
        return 1, events, unit_empty(unit)
    previous = cpu_sample()
    next_sample = time.monotonic() + 1
    quota_verified = False
    pulse_identity = None
    next_pulse = 0
    with prompt.open("rb") as input_, selectors.DefaultSelector() as selector:
        process = subprocess.Popen(
            command,
            stdin=input_,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
            },
        )
        for name, pipe in [("stdout", process.stdout), ("stderr", process.stderr)]:
            selector.register(pipe, selectors.EVENT_READ, name)
        try:
            while selector.get_map() or process.poll() is None:
                run = store.checkpoint(run_id)
                if int(store.clock()) >= run["deadline_at"]:
                    store.stop(run_id, "timed_out")
                if events.invalid:
                    store.stop(run_id, "output_limit")
                now = time.monotonic()
                if now >= next_sample:
                    current = cpu_sample()
                    total = current[0] - previous[0]
                    busy = 100 * (1 - (current[1] - previous[1]) / total) if total else 0
                    previous = current
                    next_sample = now + 1
                    if busy > 80:
                        store.stop(run_id, "host_cpu_limit")
                if not quota_verified:
                    result = subprocess.run(
                        ["systemctl", "show", unit, "-p", "ControlGroup", "--value"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    group = result.stdout.strip()
                    quota_path = Path("/sys/fs/cgroup") / group.lstrip("/") / "cpu.max"
                    if group and quota_path.exists():
                        quota, period = quota_path.read_text().split()
                        quota_verified = quota != "max" and int(quota) / int(period) <= 0.5
                        if not quota_verified:
                            store.stop(run_id, "interrupted")
                        else:
                            pulse_identity = store.started(run_id)
                            next_pulse = now + PULSE_INTERVAL
                if pulse_identity and now >= next_pulse and process.poll() is None:
                    store.heartbeat(run_id, pulse_identity)
                    next_pulse = now + PULSE_INTERVAL
                if store.get(run_id)["stop_reason"] and process.poll() is None:
                    if not stop_unit(unit):
                        return 1, events, False
                for key, _ in selector.select(0.2):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        events.feed(data, stdout=key.data == "stdout")
            if events.buffer.strip():
                events.invalid = True
            return process.wait(), events, quota_verified and unit_empty(unit)
        finally:
            if process.poll() is None:
                stop_unit(unit)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for pipe in [process.stdout, process.stderr]:
                pipe.close()

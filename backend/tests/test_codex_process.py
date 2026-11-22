import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from milo_home.codex_process import DirectUnit, cpu_set
from milo_home.codex_rpc import ProtocolError


def fake_group(tmp_path, *, populated="0", state="inactive"):
    group = tmp_path / "test.service"
    group.mkdir()
    (group / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n")
    (group / "cpu.max").write_text("5000 10000\n")
    (group / "memory.max").write_text("805306368\n")
    (group / "pids.max").write_text("64\n")
    (group / "cgroup.procs").write_text("42\n")
    (group / "cpuset.cpus.effective").write_text(
        ",".join(map(str, sorted(os.sched_getaffinity(0))))
    )
    properties = {
        "LoadState": "loaded",
        "ActiveState": state,
        "ControlGroup": "/test.service",
        "InvocationID": "a" * 32,
        "MainPID": "42",
    }

    def run(*args, **kwargs):
        return SimpleNamespace(
            returncode=0, stdout="\n".join(f"{k}={v}" for k, v in properties.items())
        )

    unit = DirectUnit(seconds=30, run=run, cgroup_root=tmp_path)
    return unit, properties, group


def test_cgroup_hierarchical_population_and_service_state_both_required(tmp_path):
    unit, properties, group = fake_group(tmp_path)
    assert not unit.empty(), "Never observed execution cannot be called stopped"
    properties["ActiveState"] = "active"
    assert unit.ready()
    assert not unit.empty()
    properties["ActiveState"] = "inactive"
    (group / "cgroup.events").write_text("populated 1\n")
    assert not unit.empty(), "Nested descendants keep the hierarchy populated"
    (group / "cgroup.events").write_text("populated 0\n")
    assert unit.empty()
    (group / "cgroup.events").unlink()
    assert not unit.empty(), "Missing evidence is not an empty group"


def test_changed_group_or_failed_bus_preserves_unknown(tmp_path):
    unit, properties, _ = fake_group(tmp_path, state="active")
    assert unit.ready()
    properties.update(ActiveState="inactive", ControlGroup="/replacement.service")
    assert not unit.empty()

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("systemctl", 3)

    unit.run = timeout
    assert not unit.stop()
    assert not unit.empty()


@pytest.mark.parametrize(
    "file,value",
    [
        ("cpu.max", "max 10000"),
        ("cpu.max", "50000 100000"),
        ("memory.max", "max"),
        ("pids.max", "max"),
    ],
)
def test_model_cannot_start_before_resource_quota_is_verified(tmp_path, file, value):
    unit, _, group = fake_group(tmp_path, state="active")
    (group / file).write_text(value)
    assert not unit.ready()
    assert not unit.observed


def test_window_is_absolute_and_cpu_guard_does_not_change_other_services(monkeypatch):
    unit = DirectUnit(seconds=30)
    unit.deadline = time.monotonic() - 1
    with pytest.raises(ProtocolError, match="expired"):
        unit.checkpoint()
    unit.deadline = time.monotonic() + 30
    unit.previous_cpu = (100, 100)
    unit.next_cpu = 0
    monkeypatch.setattr(unit, "_cpu_sample", lambda: (200, 110))
    with pytest.raises(ProtocolError, match="CPU limit"):
        unit.checkpoint()


@pytest.mark.parametrize("change", ["invocation", "pid", "membership"])
def test_process_identity_changes_do_not_reuse_ready_evidence(tmp_path, change):
    unit, properties, group = fake_group(tmp_path, state="active")
    assert unit.ready()
    if change == "invocation":
        properties["InvocationID"] = "b" * 32
    elif change == "pid":
        properties["MainPID"] = "43"
        (group / "cgroup.procs").write_text("43\n")
    else:
        (group / "cgroup.procs").write_text("43\n")
    assert not unit.ready()
    properties["ActiveState"] = "inactive"
    assert not unit.empty()


def test_stop_refuses_replacement_invocation_before_sending_stop(tmp_path):
    unit, properties, _ = fake_group(tmp_path, state="active")
    assert unit.ready()
    original = unit.run
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return original(command, **kwargs)

    unit.run = run
    properties["InvocationID"] = "b" * 32
    assert not unit.stop()
    assert all(command[1] != "stop" for command in calls)


@pytest.mark.parametrize("value", ["", "0-", "3-1", "0-65536", "0,,1", "-1"])
def test_invalid_cpu_evidence_is_rejected(value):
    with pytest.raises(ValueError):
        cpu_set(value)


def test_broader_effective_cpu_set_cannot_be_called_ready(tmp_path):
    unit, _, group = fake_group(tmp_path, state="active")
    assert cpu_set("0-2,4") == {0, 1, 2, 4}
    (group / "cpuset.cpus.effective").write_text(str(max(unit.allowed_cpus) + 1))
    assert not unit.ready() and not unit.observed


REAL_SYSTEMD = pytest.mark.skipif(
    os.getuid() != 0 or not Path("/run/systemd/system").exists(),
    reason="Requires local root/systemd; synthetic commands only",
)


@REAL_SYSTEMD
@pytest.mark.parametrize("natural_expiry", [False, True])
def test_real_host_family_stops_session_descendants(tmp_path, natural_expiry):
    pids_file = tmp_path / "pids.json"
    # setsid escapes a Unix process group, but remains in the systemd execution family.
    code = (
        "import os,json,subprocess,time; from pathlib import Path; "
        "child=subprocess.Popen(['/usr/bin/sleep','120'],start_new_session=True); "
        f"Path({str(pids_file)!r}).write_text(json.dumps([os.getpid(),child.pid])); "
        "time.sleep(120)"
    )
    unit = DirectUnit(seconds=4 if natural_expiry else 15)
    process = unit.launch([sys.executable, "-c", code])
    try:
        unit.wait_ready()
        assert cpu_set((unit.group / "cpuset.cpus.effective").read_text()) <= unit.allowed_cpus
        end = time.monotonic() + 2
        while not pids_file.exists() and time.monotonic() < end:
            unit.checkpoint()
            time.sleep(0.02)
        pids = json.loads(pids_file.read_text())
        identities = {}
        for pid in pids:
            fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
            identities[pid] = fields[19]
        assert len(identities) == 2
        if natural_expiry:
            process.wait(timeout=6)
            assert unit.empty()
        else:
            assert unit.stop()
        for pid, started in identities.items():
            path = Path("/proc") / str(pid) / "stat"
            if path.exists():
                fields = path.read_text().rsplit(") ", 1)[1].split()
                assert fields[19] != started or fields[0] == "Z"
        assert unit.empty()
    finally:
        unit.stop()
        for pipe in [process.stdin, process.stdout, process.stderr]:
            pipe.close()

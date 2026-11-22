import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from milo_home.codex_process import DirectUnit
from milo_home.codex_rpc import ProtocolError
from milo_home.work_stop import boot_id, identity_shape, recorded_identity, stop_family, stop_shape

UNIT = "milo-work-" + "a" * 32 + ".service"


@pytest.fixture
def stop_case(tmp_path):
    identity = {
        "unit": UNIT,
        "group": "/system.slice/" + UNIT,
        "boot_id": boot_id(),
        "invocation": "b" * 32,
        "main_pid": 42,
    }
    group = tmp_path / identity["group"].lstrip("/")
    group.mkdir(parents=True)
    (group / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    values = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "MainPID": "42",
        "InvocationID": identity["invocation"],
        "ControlGroup": identity["group"],
    }
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "stop":
            values.update(ActiveState="inactive", MainPID="0")
        return SimpleNamespace(
            returncode=0, stdout="\n".join(f"{k}={v}" for k, v in values.items())
        )

    return identity, group, values, calls, run


def test_fresh_stop_checks_before_and_after_and_hierarchical_empty(stop_case, tmp_path):
    identity, _, _, calls, run = stop_case
    assert stop_family(identity, run=run, cgroup_root=tmp_path)
    assert [c[1] for c in calls] == ["show", "stop", "show"]


@pytest.mark.parametrize("change", ["invocation", "pid", "group", "boot", "missing-invocation"])
def test_changed_identity_never_sends_stop(stop_case, tmp_path, change):
    identity, _, values, calls, run = stop_case
    if change == "invocation":
        values["InvocationID"] = "c" * 32
    elif change == "missing-invocation":
        values["InvocationID"] = ""
    elif change == "pid":
        values["MainPID"] = "43"
    elif change == "group":
        values["ControlGroup"] = "/replacement.service"
    else:
        identity["boot_id"] = "10000000-0000-4000-8000-000000000001"
    assert not stop_family(identity, run=run, cgroup_root=tmp_path)
    assert all(c[1] != "stop" for c in calls)


@pytest.mark.parametrize("change", ["populated", "missing-events", "replaced-after-stop", "bus"])
def test_uncertain_stop_does_not_claim_family_exit(stop_case, tmp_path, change):
    identity, group, values, _, original = stop_case
    if change == "populated":
        (group / "cgroup.events").write_text("populated 1\n")
    elif change == "missing-events":
        (group / "cgroup.events").unlink()

    def run(command, **kwargs):
        if change == "bus":
            raise subprocess.TimeoutExpired("synthetic", 3)
        result = original(command, **kwargs)
        if command[1] == "stop" and change == "replaced-after-stop":
            values["InvocationID"] = "c" * 32
        return result

    assert not stop_family(identity, run=run, cgroup_root=tmp_path)


def test_collected_unit_needs_fresh_not_found_and_no_populated_cgroup(stop_case, tmp_path):
    identity, group, values, calls, run = stop_case
    values.update(
        LoadState="not-found", ActiveState="inactive", MainPID="0", InvocationID="", ControlGroup=""
    )
    assert stop_family(identity, run=run, cgroup_root=tmp_path)
    (group / "cgroup.events").write_text("populated 1\n")
    assert not stop_family(identity, run=run, cgroup_root=tmp_path)
    (group / "cgroup.events").unlink()
    group.rmdir()
    assert stop_family(identity, run=run, cgroup_root=tmp_path)
    assert all(c[1] != "stop" for c in calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("group", "/system.slice/../" + UNIT),
        ("group", "/unrelated.service"),
        ("main_pid", True),
        ("invocation", "bad"),
    ],
)
def test_malformed_identity_is_rejected(stop_case, field, value):
    with pytest.raises(ProtocolError):
        identity_shape(stop_case[0] | {field: value})


def test_old_or_duplicate_ready_and_invalid_stop_record_are_not_invented(stop_case):
    identity = stop_case[0]
    ready = {"kind": "process", "phase": "ready", **identity}
    assert recorded_identity([(1, ready)], UNIT) == identity
    stop_shape({"kind": "work_stop", **identity, "checked_at": 1})
    for entries in [
        [],
        [(1, {k: v for k, v in ready.items() if k != "group"})],
        [(1, ready), (2, ready)],
    ]:
        with pytest.raises(ProtocolError):
            recorded_identity(entries, UNIT)
    with pytest.raises(ProtocolError):
        stop_shape({"kind": "work_stop", **identity, "checked_at": True})


@pytest.mark.skipif(
    os.geteuid() != 0 or not Path("/run/systemd/system").exists(),
    reason="Requires root/systemd; synthetic processes only",
)
def test_reopened_stopper_stops_actual_family_with_setsid_descendant(tmp_path):
    marker = tmp_path / "pids.json"
    code = (
        "import os,json,subprocess,time; from pathlib import Path; "
        "p=subprocess.Popen(['/usr/bin/sleep','120'],start_new_session=True); "
        f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),p.pid])); time.sleep(120)"
    )
    unit = DirectUnit(seconds=15)
    process = unit.launch([sys.executable, "-c", code])
    try:
        unit.wait_ready()
        identity = {
            "unit": unit.name,
            "invocation": unit.invocation,
            "main_pid": unit.main_pid,
            "boot_id": boot_id(),
            "group": "/" + str(unit.group.relative_to(unit.cgroup_root)),
        }
        until = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < until:
            time.sleep(0.02)
        assert len(json.loads(marker.read_text())) == 2
        assert stop_family(identity)
        process.wait(timeout=4)
        for pid in json.loads(marker.read_text()):
            stat = Path("/proc", str(pid), "stat")
            if stat.exists():
                assert stat.read_text().rsplit(") ", 1)[1].split()[0] == "Z"
        assert stop_family(identity)
    finally:
        unit.stop()
        for pipe in [process.stdin, process.stdout, process.stderr]:
            pipe.close()

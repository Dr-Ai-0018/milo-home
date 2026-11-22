"""Unit state and capture failure boundaries without touching host services."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_database_identity import owned_database as owned_database

from milo_home.database_identity import ServiceDatabase
from milo_home.maintenance_capture import capture_for_maintenance
from milo_home.maintenance_policy import CANDIDATE_BUDGET, BackupPolicy
from milo_home.maintenance_units import (
    MaintenanceUnits,
    maintenance_lock,
    restore_maintenance_entries,
)


class Units:
    def __init__(self, units):
        self.calls = []
        self.values = {
            unit: {
                "LoadState": "loaded",
                "ActiveState": (
                    "active" if unit in (*units.intake_timers, units.api) else "inactive"
                ),
                "MainPID": "0",
            }
            for unit in (*units.intake_timers, *units.workers, units.api)
        }
        for unit in units.intake_timers:
            del self.values[unit]["MainPID"]
        self.fail_start = set()

    def __call__(self, action, unit):
        self.calls.append((action, unit))
        if action == "show":
            return dict(self.values[unit])
        if action == "start" and unit in self.fail_start:
            raise RuntimeError("synthetic start failure")
        self.values[unit]["ActiveState"] = "active" if action == "start" else "inactive"


@pytest.fixture
def maintenance_case(private_runtime_directory, monkeypatch):
    import milo_home.maintenance_capture as module

    root = private_runtime_directory("milo-maintenance-units-")
    units = MaintenanceUnits()
    run = Units(units)
    options = {
        "database": SimpleNamespace(path=Path("/not-used")),
        "home": root / "home",
        "workspace": root / "workspace",
        "output": root / "archive",
        "policy": BackupPolicy(CANDIDATE_BUDGET),
        "units": units,
        "unit_runner": run,
        "writers_stopped": True,
        "clock": lambda: 1800000000,
    }
    monkeypatch.setattr(module, "preflight_backup", lambda *a, **kw: {"ready": True})
    monkeypatch.setattr(module, "_quiescent", lambda *a: True)
    monkeypatch.setattr(
        module,
        "capture_generation",
        lambda *a, **kw: {"generation": "a" * 32, "manifest_sha256": "b" * 64},
    )
    return root, units, run, options


def test_capture_restores_api_before_only_originally_active_timer(maintenance_case, monkeypatch):
    import milo_home.maintenance_capture as module

    root, units, run, options = maintenance_case
    off = units.intake_timers[1]
    run.values[off]["ActiveState"] = "inactive"

    def capture(*args, **kwargs):
        assert kwargs["budget"] == CANDIDATE_BUDGET
        assert kwargs["writers_stopped"]
        state = json.loads((root / "archive.maintenance.json").read_bytes())
        assert state["paused"] == [units.intake_timers[0], units.api]
        assert all(run.values[name]["ActiveState"] == "inactive" for name in state["paused"])
        return {"generation": "a" * 32}

    monkeypatch.setattr(module, "capture_generation", capture)
    result = capture_for_maintenance(**options)
    assert result["status"] == "captured"
    assert result["snapshot_id"] is None
    starts = [unit for action, unit in run.calls if action == "start"]
    assert starts == [units.api, units.intake_timers[0]]
    assert ("stop", off) not in run.calls
    assert all(("stop", worker) not in run.calls for worker in units.workers)


def test_busy_worker_defers_without_stopping_api_or_killing_worker(maintenance_case):
    _, units, run, options = maintenance_case
    run.values[units.workers[0]]["ActiveState"] = "active"
    result = capture_for_maintenance(**options)
    assert result["status"] == "deferred"
    assert result["reason"] == "execution_busy_or_unknown"
    assert ("stop", units.api) not in run.calls
    assert all(("stop", worker) not in run.calls for worker in units.workers)
    assert result["entries"]["restore_pending"] == []


def test_timer_without_mainpid_pauses_and_restores(maintenance_case):
    _, units, run, options = maintenance_case
    assert all("MainPID" not in run.values[unit] for unit in units.intake_timers)
    result = capture_for_maintenance(**options)
    assert result["status"] == "captured"
    for unit in units.intake_timers:
        assert ("stop", unit) in run.calls and ("start", unit) in run.calls
        assert run.values[unit]["ActiveState"] == "active"
    assert result["entries"]["restore_pending"] == []


@pytest.mark.parametrize("kind", ["worker", "api"])
def test_service_without_mainpid_remains_unconfirmed(maintenance_case, kind):
    _, units, run, options = maintenance_case
    service = units.workers[0] if kind == "worker" else units.api
    del run.values[service]["MainPID"]
    result = capture_for_maintenance(**options)
    assert result["status"] != "captured"
    assert ("start", service) not in run.calls
    if kind == "worker":
        assert result["reason"] == "execution_busy_or_unknown"
        assert ("stop", units.api) not in run.calls
    else:
        assert units.api in result["entries"]["restore_pending"]
        assert all(("start", unit) not in run.calls for unit in units.intake_timers)


def test_missing_writer_confirmation_and_unknown_units_do_not_mutate(maintenance_case):
    _, units, run, options = maintenance_case
    result = capture_for_maintenance(**{**options, "writers_stopped": False})
    assert result["reason"] == "writers_unconfirmed" and not run.calls
    run.values[units.api]["ActiveState"] = "deactivating"
    result = capture_for_maintenance(**options)
    assert result["status"] == "failed"
    assert not [call for call in run.calls if call[0] in {"start", "stop"}]


@pytest.mark.parametrize("interrupted", [False, True])
def test_capture_failure_or_interrupt_restores_original_entries(
    maintenance_case, monkeypatch, interrupted
):
    import milo_home.maintenance_capture as module

    root, units, run, options = maintenance_case

    def fail(*args, **kwargs):
        if interrupted:
            raise KeyboardInterrupt
        raise ValueError("synthetic source changed")

    monkeypatch.setattr(module, "capture_generation", fail)
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            capture_for_maintenance(**options)
    else:
        assert capture_for_maintenance(**options)["status"] == "failed"
    state = json.loads((root / "archive.maintenance.json").read_bytes())
    assert state["phase"] == "restored"
    assert run.values[units.api]["ActiveState"] == "active"
    assert all(run.values[name]["ActiveState"] == "active" for name in units.intake_timers)


def test_unconfirmed_api_restore_keeps_timers_stopped_and_has_explicit_recovery(maintenance_case):
    root, units, run, options = maintenance_case
    run.fail_start.add(units.api)
    result = capture_for_maintenance(**options)
    assert result["status"] == "failed" and result["reason"] == "entry_restore_incomplete"
    assert result["capture"]["generation"] == "a" * 32
    assert result["entries"]["restore_pending"] == [units.api, *units.intake_timers]
    assert all(run.values[name]["ActiveState"] == "inactive" for name in units.intake_timers)
    run.fail_start.clear()
    restored = restore_maintenance_entries(
        root / "archive.maintenance.json", units=units, unit_runner=run
    )
    assert restored["restore_pending"] == []
    assert run.values[units.api]["ActiveState"] == "active"


def test_maintenance_lock_prevents_competing_state_restore(maintenance_case):
    _, units, run, options = maintenance_case
    with maintenance_lock(units):
        result = capture_for_maintenance(**options)
    assert result["reason"] == "maintenance_busy"
    assert not run.calls


def test_service_database_restores_root_before_reading_private_process_evidence(
    owned_database, private_runtime_directory, monkeypatch
):
    import milo_home.maintenance_capture as module

    path, account = owned_database
    home = private_runtime_directory("milo-maintenance-owner-")
    evidence = home / "synthetic-root-only"
    evidence.write_bytes(b"root evidence")
    evidence.chmod(0o600)
    observed = []

    def inspect(directory, **kwargs):
        assert os.geteuid() == 0
        assert (directory / evidence.name).read_bytes() == b"root evidence"
        observed.append(True)
        return {"observations": []}

    monkeypatch.setattr(module, "observe_recorded", inspect)
    assert module._quiescent(
        ServiceDatabase(path, account.pw_name), home, BackupPolicy(CANDIDATE_BUDGET)
    )
    assert observed == [True]
    assert os.geteuid() == 0
    assert all(item.stat().st_uid == account.pw_uid for item in path.parent.iterdir())

import hashlib
import json
import subprocess
from copy import deepcopy

import pytest
from test_codex_journal import unit
from test_journal_segments import archive, prepare_stream
from test_work_runtime import journal_at

from milo_home.backup_process_evidence import observe_recorded, verify_observation_sources
from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.journal_storage import SEGMENTS, canonical
from milo_home.runtime_evidence_backup import capture_runtime, rehearse_runtime, verified_runtime
from milo_home.work_stop import boot_id


@pytest.fixture
def cold_case(private_runtime_directory):
    root = private_runtime_directory("milo-segment-backup-")
    home = root / "home"
    journal = journal_at(home, root)
    case = prepare_stream(journal)
    archive(case)
    return root, home, journal, case


def test_runtime_v3_restores_base_and_overlay_without_original_home(cold_case):
    root, home, journal, case = cold_case
    stop = {"kind": "work_stop", **case[2]["scope"]["process_identity"], "checked_at": 101}
    journal.record(case[1], stop)
    expected = journal.stream_snapshot(case[1])
    storage = journal.storage_summary()
    result = capture_runtime(home, root / "archive", writers_stopped=True)
    manifest = verified_runtime(root / "archive", result["manifest_sha256"], 256 * 1024 * 1024)
    assert manifest["version"] == 3 and manifest["native_evidence"] is None
    assert manifest["storage"] == storage
    home.rename(root / "original-home-unavailable")
    report = rehearse_runtime(root / "archive", root / "restored", result["manifest_sha256"])
    assert report["restore_verified"] and not report["automatic_resume"]
    restored = PrivateJournal(root / "restored", initialize=False, read_only=True)
    assert restored.stream_snapshot(case[1]) == expected
    assert restored.storage_summary() == storage
    assert restored.binding_record()["codex_home"] == str(home)


@pytest.mark.parametrize("damage", ["downgrade", "summary", "extra", "missing"])
def test_v3_manifest_cannot_hide_or_drop_segment_history(cold_case, damage):
    root, home, _, case = cold_case
    result = capture_runtime(home, root / "archive", writers_stopped=True)
    manifest_path = root / "archive/manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    if damage == "downgrade":
        manifest["version"] = 2
        manifest.pop("storage")
    elif damage == "summary":
        manifest["storage"]["logical_sha256"] = "0" * 64
    elif damage == "extra":
        path = root / "archive/evidence" / SEGMENTS / ("f" * 32 + ".json")
        path.write_bytes(b"{}")
        path.chmod(0o600)
    else:
        (root / "archive/evidence" / SEGMENTS / (case[1] + ".json")).unlink()
    content = canonical(manifest)
    manifest_path.write_bytes(content)
    with pytest.raises((ValueError, OSError, ProtocolError)):
        verified_runtime(root / "archive", hashlib.sha256(content).hexdigest(), 256 * 1024 * 1024)
    assert result["captured"]


def test_many_archived_families_are_complete_history_not_fresh_absence(private_runtime_directory):
    root = private_runtime_directory("milo-many-segments-")
    home = root / "home"
    journal = journal_at(home, root)
    for index in range(1, 66):
        archive(prepare_stream(journal, index))
    stream = journal.open_stream(unit(1000))
    journal.record(
        stream,
        {
            "kind": "process",
            "phase": "ready",
            "unit": unit(1000),
            "group": "/synthetic/" + unit(1000),
            "boot_id": boot_id(),
            "invocation": "a" * 32,
            "main_pid": 42,
        },
    )
    calls = []

    def run(command, **options):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=(
                "LoadState=not-found\nActiveState=inactive\nMainPID=0\nInvocationID=\nControlGroup=\n"
            ),
            stderr="",
        )

    report = observe_recorded(home, run=run, cgroup_root=root / "cgroups")
    assert report["version"] == 2 and report["archived_count"] == 65
    assert len(report["archived_families"]) == 65 and len(report["families"]) == 1
    assert len(calls) == 2 and report["online_families_absent_observed"]
    assert not report["recorded_families_absent_observed"]
    assert all(family["fresh_observation"] is False for family in report["archived_families"])
    capture_runtime(home, root / "runtime", writers_stopped=True)
    manifest = {
        "process_observations": {"before": report, "after": report},
        "quiescence_verified": False,
    }
    verify_observation_sources(root, manifest)
    changed = deepcopy(manifest)
    changed["process_observations"]["after"]["archived_families"].pop()
    with pytest.raises(ValueError, match="observations differ"):
        verify_observation_sources(root, changed)


def test_historical_traversal_does_not_consume_online_deadline(cold_case, monkeypatch):
    from milo_home import backup_process_evidence as module

    _, home, _, _ = cold_case
    elapsed = [0]
    original = module._observation_scope

    def slow_scope(home, **options):
        result = original(home, **options)
        elapsed[0] += 30
        return [
            {
                "identity": result[1][0]["identity"],
                "gap": None,
            }
        ], result[1]

    observed = []
    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(module, "_observation_scope", slow_scope)

    def observe(identity, **options):
        observed.append(identity)
        return {"absent_observed": False, "reason": "synthetic-observation"}

    monkeypatch.setattr(module, "observe_family", observe)
    report = observe_recorded(home)
    assert len(observed) == 1
    assert report["observations"][0]["reason"] == "synthetic-observation"


def test_observation_scope_and_verification_respect_callers_budget(cold_case):
    root, home, _, _ = cold_case
    with pytest.raises(ProtocolError, match="budget"):
        observe_recorded(home, budget=1)
    capture_runtime(home, root / "runtime", writers_stopped=True)
    report = observe_recorded(home)
    manifest = {
        "process_observations": {"before": report, "after": report},
        "quiescence_verified": False,
    }
    with pytest.raises(ProtocolError, match="budget"):
        verify_observation_sources(root, manifest, budget=1)


def test_corrupt_cold_history_blocks_new_stream_admission(cold_case):
    _, home, journal, case = cold_case
    path = home / SEGMENTS / (case[1] + ".json")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ProtocolError, match="bytes differ"):
        journal.open_stream(unit(1001))
    assert len(journal.list_streams()) == 1

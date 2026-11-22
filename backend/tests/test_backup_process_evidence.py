"""Fresh observation never sends stop or interprets old journal status as proof."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_paired_backup import capture
from test_paired_backup import paired_case as paired_case
from test_work_stop import stop_case as stop_case
from test_work_tasks import work_case as work_case

from milo_home.backup_process_evidence import observe_family, observe_recorded, recorded_families
from milo_home.codex_process import DirectUnit
from milo_home.paired_backup import verified_generation
from milo_home.work_creation_records import CreationRecords
from milo_home.work_stop import boot_id


def historical_process(journal, stream, event):
    """Reconstruct historical rows in this deliberately sealed synthetic fixture.

    Live record() must keep refusing writes to unknown streams. This prepares
    offline input, without reopening the stream or changing its unknown request.
    """
    payload = json.dumps(event)
    with journal._connect() as connection:
        connection.execute("INSERT INTO entries(stream,payload) VALUES (?,?)", (stream, payload))
        connection.execute(
            "UPDATE streams SET bytes=bytes+? WHERE id=?", (len(payload.encode()), stream)
        )


def test_inactive_exact_generation_requires_empty_whole_group(stop_case, tmp_path):
    identity, _, values, calls, run = stop_case
    values.update(ActiveState="inactive", MainPID="0")
    report = observe_family(identity, run=run, cgroup_root=tmp_path)
    assert report["absent_observed"]
    assert report["systemd_before"] == report["systemd_after"] == values
    assert report["cgroup"] == {
        "exists": True,
        "populated": "0",
    }
    assert [call[1] for call in calls] == ["show", "show"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ("active", "family-active"),
        ("populated", "cgroup-empty-unconfirmed"),
        ("invocation", "identity-unconfirmed"),
        ("pid", "identity-unconfirmed"),
        ("group", "identity-unconfirmed"),
        ("boot", "boot-changed"),
        ("missing-events", "observation-unavailable"),
        ("missing-group", "cgroup-empty-unconfirmed"),
        ("bus", "observation-unavailable"),
        ("restart-between-samples", "identity-unconfirmed"),
        ("reboot-between-samples", "boot-changed"),
    ],
)
def test_missing_or_changed_evidence_never_claims_absence(
    stop_case, tmp_path, monkeypatch, change, reason
):
    identity, group, values, calls, original = stop_case
    values.update(ActiveState="inactive", MainPID="0")
    if change == "active":
        values.update(ActiveState="active", MainPID="42")
    elif change == "populated":
        (group / "cgroup.events").write_text("populated 1\n")
    elif change == "invocation":
        values["InvocationID"] = "c" * 32
    elif change == "pid":
        values["MainPID"] = "43"
    elif change == "group":
        values["ControlGroup"] = "/other.service"
    elif change == "boot":
        identity["boot_id"] = str(uuid4())
    elif change in {"missing-events", "missing-group"}:
        (group / "cgroup.events").unlink()
        if change == "missing-group":
            group.rmdir()
    if change == "reboot-between-samples":
        readings = iter([identity["boot_id"], str(uuid4())])
        monkeypatch.setattr("milo_home.backup_process_evidence.boot_id", lambda: next(readings))

    def run(command, **options):
        if change == "bus":
            raise subprocess.TimeoutExpired("synthetic-show", 3)
        if change == "restart-between-samples" and calls:
            values["InvocationID"] = "c" * 32
        return original(command, **options)

    report = observe_family(identity, run=run, cgroup_root=tmp_path)
    assert not report["absent_observed"] and report["reason"] == reason
    assert all(call[1] == "show" for call in calls)


def test_garbage_collected_unit_needs_fresh_not_found(stop_case, tmp_path):
    identity, group, values, _, run = stop_case
    values.update(
        LoadState="not-found", ActiveState="inactive", MainPID="0", InvocationID="", ControlGroup=""
    )
    (group / "cgroup.events").unlink()
    group.rmdir()
    assert observe_family(identity, run=run, cgroup_root=tmp_path)["absent_observed"]


def test_all_streams_and_creation_connections_include_missing_and_duplicate_ready(
    paired_case, stop_case
):
    _, _, home, _, journal, intent = paired_case
    identity = stop_case[0] | {
        "unit": intent.unit_name,
        "group": "/system.slice/" + intent.unit_name,
    }
    missing = recorded_families(home)
    assert missing[0]["identity"] is None and missing[0]["gap"] == "missing-or-duplicate-ready"
    historical_process(
        journal,
        intent.stream_id,
        {
            "kind": "process",
            "phase": "ready",
            **identity,
        },
    )
    families = recorded_families(home)
    assert families[0]["identity"] == identity
    records = CreationRecords(home, create=True)
    creation_unit = "milo-work-" + uuid4().hex + ".service"
    records.record(
        {
            "kind": "process",
            "phase": "launch_intent",
            "connection": creation_unit,
        }
    )
    assert recorded_families(home)[1]["gap"] == "missing-or-duplicate-ready"
    historical_process(
        journal,
        intent.stream_id,
        {
            "kind": "process",
            "phase": "ready",
            **identity,
        },
    )
    assert recorded_families(home)[0]["gap"] == "missing-or-duplicate-ready"


def test_old_stop_confirmed_does_not_replace_fresh_observation(paired_case, stop_case, tmp_path):
    _, _, home, _, journal, intent = paired_case
    identity, _, values, calls, run = stop_case
    identity = identity | {
        "unit": intent.unit_name,
        "group": "/system.slice/" + intent.unit_name,
    }
    historical_process(
        journal,
        intent.stream_id,
        {
            "kind": "process",
            "phase": "ready",
            **identity,
        },
    )
    historical_process(
        journal,
        intent.stream_id,
        {
            "kind": "process",
            "phase": "stop_confirmed",
            "unit": intent.unit_name,
        },
    )
    values.update(InvocationID=identity["invocation"], ControlGroup=identity["group"])
    report = observe_recorded(home, run=run, cgroup_root=tmp_path)
    assert not report["recorded_families_absent_observed"]
    assert report["observations"][0]["reason"] == "family-active"
    assert report["quiescence_verified"] is False
    assert len(report["unverified_writers"]) == 3
    assert all(call[1] == "show" for call in calls)


def test_global_deadline_retains_unchecked_families(paired_case, stop_case, monkeypatch):
    from milo_home import backup_process_evidence as module

    _, _, home, _, _, _ = paired_case
    family = {
        "identity": stop_case[0],
        "gap": None,
    }
    monkeypatch.setattr(module, "_observation_scope", lambda _, **options: ([family, family], None))
    readings = iter([0, 11, 12])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(readings))

    def forbidden(*args, **kwargs):
        raise AssertionError("Deadline must prevent systemctl")

    report = observe_recorded(home, run=forbidden)
    assert [item["reason"] for item in report["observations"]] == ["observation-deadline"] * 2
    assert not report["recorded_families_absent_observed"]


def test_creation_choice_without_process_records_is_explicit_gap(paired_case):
    _, _, home, _, _, _ = paired_case
    (home / "main-creation.json").write_text('{"synthetic":"incomplete"}\n')
    family = recorded_families(home)[-1]
    assert family["source"] == "creation" and family["identity"] is None
    assert family["gap"] == "missing-creation-process-records"


def test_duplicate_units_and_family_bound_are_not_treated_as_complete(paired_case, monkeypatch):
    from milo_home import backup_process_evidence as module

    _, _, home, _, _, intent = paired_case
    records = CreationRecords(home, create=True)
    records.record(
        {
            "kind": "process",
            "phase": "launch_intent",
            "connection": intent.unit_name,
        }
    )
    assert {family["gap"] for family in recorded_families(home)} == {"duplicate-unit"}
    monkeypatch.setattr(module, "MAX_FAMILIES", 1)
    with pytest.raises(ValueError, match="family bound"):
        recorded_families(home)


def test_closed_streams_are_still_part_of_the_observed_scope(paired_case):
    _, _, home, _, journal, _ = paired_case
    reference = str(uuid4())
    with journal._connect() as connection:
        connection.execute(
            "INSERT INTO streams(id,unit,state) VALUES (?,?,'closed')",
            (reference, "milo-work-" + uuid4().hex + ".service"),
        )
    families = recorded_families(home)
    assert len(families) == 2
    assert next(f for f in families if f["reference"] == reference)["identity"] is None


def test_empty_recorded_scope_does_not_assert_all_writers_stopped(paired_case, monkeypatch):
    _, _, home, _, _, _ = paired_case
    monkeypatch.setattr(
        "milo_home.backup_process_evidence._observation_scope", lambda _, **options: ([], None)
    )
    report = observe_recorded(home)
    assert report["families"] == report["observations"] == []
    assert not report["recorded_families_absent_observed"]
    assert not report["quiescence_verified"]


@pytest.mark.parametrize("field,value", [("group", "/wrong.service"), ("boot_id", None)])
def test_incomplete_ready_identity_is_retained_as_gap(paired_case, stop_case, field, value):
    _, _, home, _, journal, intent = paired_case
    identity = stop_case[0] | {
        "unit": intent.unit_name,
        "group": "/system.slice/" + intent.unit_name,
        field: value,
    }
    historical_process(
        journal,
        intent.stream_id,
        {
            "kind": "process",
            "phase": "ready",
            **identity,
        },
    )
    family = recorded_families(home)[0]
    assert family["identity"] is None and family["gap"] == "invalid-ready-identity"


def test_paired_generation_binds_both_observations_to_captured_records(paired_case):
    root, _, _, _, journal, intent = paired_case
    original = journal.work_snapshot(intent.stream_id)
    result = capture(paired_case)
    manifest = verified_generation(root / "archive", result["manifest_sha256"], 64 * 1024 * 1024)
    evidence = manifest["process_observations"]
    assert evidence["before"]["families"] == evidence["after"]["families"]
    assert evidence["before"]["observations"][0]["reason"] == "missing-or-duplicate-ready"
    assert journal.work_snapshot(intent.stream_id) == original
    manifest["process_observations"]["after"]["families"] = []
    content = json.dumps(manifest).encode()
    (root / "archive/manifest.json").write_bytes(content)
    with pytest.raises(ValueError, match="captured original families"):
        verified_generation(root / "archive", hashlib.sha256(content).hexdigest(), 64 * 1024 * 1024)
    # Version-one archives made before observation support remain file-only.
    del manifest["process_observations"]
    content = json.dumps(manifest).encode()
    (root / "archive/manifest.json").write_bytes(content)
    old = verified_generation(
        root / "archive", hashlib.sha256(content).hexdigest(), 64 * 1024 * 1024
    )
    assert not old["quiescence_verified"]


def test_positive_observation_keeps_unknown_and_rejects_unsupported_exit_claims(
    paired_case, stop_case, tmp_path, monkeypatch
):
    from milo_home import paired_backup as module

    root, _, _, _, journal, intent = paired_case
    identity, _, values, _, run = stop_case
    identity = identity | {
        "unit": intent.unit_name,
        "group": "/system.slice/" + intent.unit_name,
    }
    group = tmp_path / identity["group"].lstrip("/")
    group.mkdir()
    (group / "cgroup.events").write_text("populated 0\n")
    values.update(ActiveState="inactive", MainPID="0", ControlGroup=identity["group"])
    historical_process(
        journal,
        intent.stream_id,
        {
            "kind": "process",
            "phase": "ready",
            **identity,
        },
    )
    original = journal.work_snapshot(intent.stream_id)
    monkeypatch.setattr(
        module,
        "observe_recorded",
        lambda home, **options: observe_recorded(home, run=run, cgroup_root=tmp_path, **options),
    )
    captured = capture(
        paired_case,
        exclude=[
            "system.slice/" + unit + "/cgroup.events"
            for unit in (stop_case[0]["unit"], intent.unit_name)
        ],
    )
    manifest = verified_generation(root / "archive", captured["manifest_sha256"], 64 * 1024 * 1024)
    assert manifest["process_observations"]["before"]["recorded_families_absent_observed"]
    assert manifest["process_observations"]["after"]["recorded_families_absent_observed"]
    assert not manifest["quiescence_verified"]
    assert journal.work_snapshot(intent.stream_id) == original
    assert journal.outstanding()[0]["delivery"] == "unknown"
    for field in ("boot_after", "systemd_after", "cgroup", "unverified_writers"):
        changed = json.loads(json.dumps(manifest))
        sample = changed["process_observations"]["after"]
        if field == "unverified_writers":
            sample[field] = []
        else:
            sample["observations"][0].pop(field)
        content = json.dumps(changed).encode()
        (root / "archive/manifest.json").write_bytes(content)
        with pytest.raises(ValueError, match="captured original families"):
            verified_generation(
                root / "archive", hashlib.sha256(content).hexdigest(), 64 * 1024 * 1024
            )


def test_changed_family_set_between_samples_keeps_generation_incomplete(paired_case, monkeypatch):
    from milo_home import paired_backup as module

    root = paired_case[0]
    original = module.observe_recorded
    calls = 0

    def changed(home, **options):
        nonlocal calls
        report = original(home, **options)
        calls += 1
        if calls == 2:
            report["families"] = []
        return report

    monkeypatch.setattr(module, "observe_recorded", changed)
    with pytest.raises(ValueError, match="families changed"):
        capture(paired_case)
    assert (root / "archive/INCOMPLETE").exists()
    assert not (root / "archive/manifest.json").exists()


@pytest.mark.skipif(not Path("/run/systemd/system").exists(), reason="Requires systemd")
def test_real_read_only_systemd_observation_of_random_absent_family():
    unit = "milo-work-" + uuid4().hex + ".service"
    identity = {
        "unit": unit,
        "group": "/system.slice/" + unit,
        "boot_id": boot_id(),
        "invocation": uuid4().hex,
        "main_pid": 2147483647,
    }
    report = observe_family(identity)
    assert report["absent_observed"], report
    assert report["systemd_before"]["LoadState"] == "not-found"
    assert report["systemd_after"]["LoadState"] == "not-found"
    assert report["cgroup"] == {
        "exists": False,
        "populated": None,
    }


@pytest.mark.skipif(
    os.geteuid() != 0 or not Path("/run/systemd/system").exists(),
    reason="Requires root/systemd; synthetic process family only",
)
def test_real_observer_reports_active_then_absent_after_test_stops_synthetic_family():
    unit = DirectUnit(seconds=15)
    code = (
        "import subprocess,time; "
        "subprocess.Popen(['/usr/bin/sleep','30'],start_new_session=True); "
        "print('READY',flush=True); time.sleep(30)"
    )
    process = unit.launch(["/usr/bin/python3", "-c", code])
    calls = []

    def read_only(command, **options):
        assert command[:2] == ["systemctl", "show"]
        calls.append(command)
        return subprocess.run(command, **options)

    try:
        unit.wait_ready()
        assert process.stdout.readline().strip() == b"READY"
        identity = {
            "unit": unit.name,
            "group": "/" + str(unit.group.relative_to(unit.cgroup_root)),
            "boot_id": boot_id(),
            "invocation": unit.invocation,
            "main_pid": unit.main_pid,
        }
        report = observe_family(identity, run=read_only)
        assert not report["absent_observed"] and report["reason"] == "family-active"
        assert report["cgroup"]["populated"] == "1"
        # Only the test owner sends stop, to its own finite synthetic family.
        assert unit.stop()
        process.wait(timeout=4)
        assert observe_family(identity, run=read_only)["absent_observed"]
        assert len(calls) == 4
    finally:
        unit.stop()
        for pipe in [process.stdin, process.stdout, process.stderr]:
            pipe.close()

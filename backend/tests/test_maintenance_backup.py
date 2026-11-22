"""Private classification, paired byte boundaries and honest daily evidence."""

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_execution_control import execution_case as execution_case
from test_paired_backup import paired_case as paired_case
from test_work_tasks import work_case as work_case

from milo_home.maintenance_backup import (
    preflight_backup,
    read_daily_backup_evidence,
    rehearse_for_maintenance,
)
from milo_home.maintenance_policy import (
    CANDIDATE_BUDGET,
    BackupPolicy,
    load_backup_policy,
)
from milo_home.paired_backup import capture_generation
from milo_home.workspace_archive_files import RESERVE

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Root-managed maintenance policy")


def policy_file(root, **changes):
    value = {
        "version": 1,
        "budget_bytes": CANDIDATE_BUDGET,
        "include": ["extra.txt"],
        "exclude": [],
        "native_exclude": [],
        **changes,
    }
    path = root / "policy.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def test_policy_requires_explicit_budget_and_protected_source(private_runtime_directory):
    root = private_runtime_directory("milo-maintenance-policy-")
    path = policy_file(root)
    policy = load_backup_policy(path)
    assert policy.budget_bytes == 4 * 1024**3
    assert policy.include == ("extra.txt",)
    assert len(policy.sha256) == 64
    value = json.loads(path.read_text())
    del value["budget_bytes"]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Unsupported"):
        load_backup_policy(path)
    policy_file(root)
    path.chmod(0o666)
    with pytest.raises(ValueError, match="root-managed"):
        load_backup_policy(path)
    path.chmod(0o600)
    linked = root / "linked.json"
    linked.symlink_to(path)
    with pytest.raises(ValueError, match="canonical"):
        load_backup_policy(linked)


@pytest.mark.parametrize(
    "changes",
    [
        {"budget_bytes": True},
        {"budget_bytes": 1024},
        {"include": ("../outside",)},
        {"include": ("auth.json",)},
        {"include": ("x",), "exclude": ("x",)},
        {"include": ("x", "x")},
    ],
)
def test_policy_rejects_ambiguous_or_unsafe_decisions(changes):
    with pytest.raises(ValueError):
        BackupPolicy(**{"budget_bytes": CANDIDATE_BUDGET, **changes})


def test_unknown_file_is_reported_before_capture_and_explicit_decision_unblocks(paired_case):
    root, database, home, workspace, _, _ = paired_case
    policy = BackupPolicy(CANDIDATE_BUDGET)
    blocked = preflight_backup(database, home, workspace, root / "archive", policy)
    assert blocked["reason"] == "classification_required"
    assert blocked["unclassified"] == ["extra.txt"]
    assert not (root / "archive").exists()
    policy = BackupPolicy(CANDIDATE_BUDGET, include=("extra.txt",))
    ready = preflight_backup(database, home, workspace, root / "archive", policy)
    assert ready["ready"]
    assert ready["estimated_peak_bytes"] > 0
    assert not (root / "archive").exists()


def test_peak_includes_four_attachment_copies_and_reserves_free_space(paired_case, monkeypatch):
    import milo_home.maintenance_backup as module

    root, database, home, workspace, _, _ = paired_case
    policy = BackupPolicy(RESERVE, include=("extra.txt",))
    monkeypatch.setattr(module, "source_state", lambda *a, **kw: {"estimated_peak_bytes": 1000})
    monkeypatch.setattr(module, "attachment_index", lambda *a: [{"size_bytes": RESERVE // 4}])
    store = SimpleNamespace(root=root / "sealed", check_workspace=lambda _: None)
    result = preflight_backup(
        database, home, workspace, root / "archive", policy, attachment_store=store
    )
    assert result["reason"] == "budget_insufficient"
    assert result["estimated_peak_bytes"] == RESERVE + 1000
    monkeypatch.setattr(module, "attachment_index", lambda *a: [])
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=RESERVE + 999))
    result = preflight_backup(database, home, workspace, root / "archive", policy)
    assert result["reason"] == "disk_insufficient"
    assert not (root / "archive").exists()


def test_existing_paired_capture_and_rehearsal_keep_the_same_policy_budget(paired_case):
    root, database, home, workspace, _, _ = paired_case
    policy = BackupPolicy(RESERVE, include=("extra.txt",))
    captured = capture_generation(
        database,
        home,
        workspace,
        root / "archive",
        writers_stopped=True,
        **policy.paired_options(),
    )
    report = rehearse_for_maintenance(
        root / "archive", root / "restored", captured["manifest_sha256"], policy
    )
    assert report["file_restore_verified"]
    assert report["generation"] == captured["generation"]
    assert not report["automatic_resume"]
    assert (root / "restored/workspace/extra.txt").read_bytes() == (
        workspace / "extra.txt"
    ).read_bytes()


def test_daily_failure_keeps_previous_success_without_inventing_snapshot(private_runtime_directory):
    root = private_runtime_directory("milo-maintenance-daily-")
    path = root / "last-success"
    path.write_text("2026-11-19T20:36:33+00:00\n")
    now = datetime(2026, 11, 21, tzinfo=UTC)
    values = {
        "LoadState": "loaded",
        "ActiveState": "failed",
        "Result": "exit-code",
        "ExecMainStatus": "1",
        "ExecMainStartTimestamp": "Fri 2026-11-20 20:33:02 UTC",
        "ExecMainExitTimestamp": "Fri 2026-11-20 20:36:33 UTC",
    }
    calls = []

    def run(action, unit):
        calls.append((action, unit))
        return values

    options = {
        "unit_runner": run,
        "success_file": path,
        "now": now,
        "unit": "synthetic-backup.service",
    }
    report = read_daily_backup_evidence(**options)
    assert report["last_outcome"] == "failed"
    assert report["last_success_at"] < report["last_attempt_at"] < report["timestamp"]
    assert report["snapshot_id"] is None and report["generation_id"] is None
    assert calls == [("show", "synthetic-backup.service")]
    values.update(ActiveState="active", Result="success", ExecMainStatus="0")
    assert read_daily_backup_evidence(**options)["last_outcome"] == "unknown"
    values.update(ActiveState="inactive")
    succeeded = read_daily_backup_evidence(**options)
    assert succeeded["last_outcome"] == "succeeded"
    assert succeeded["snapshot_id"] is None


def test_daily_missing_and_future_evidence_remain_unknown(private_runtime_directory):
    root = private_runtime_directory("milo-maintenance-daily-missing-")
    path = root / "last-success"
    now = datetime(2026, 11, 21, tzinfo=UTC)
    report = read_daily_backup_evidence(unit_runner=lambda *a: {}, success_file=path, now=now)
    assert report["last_attempt_at"] is None and report["last_success_at"] is None
    path.write_text("2099-01-01T00:00:00Z")
    report = read_daily_backup_evidence(unit_runner=lambda *a: {}, success_file=path, now=now)
    assert report["last_success_at"] is None and report["last_outcome"] == "unknown"

"""Combine actual business/journal recovery with an independent Git artifact archive."""

import os
import sqlite3

import pytest
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_backup_rehearsal import database_dump
from test_work_runtime import bind
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case
from test_workspace_backup import commit

from milo_home.backup_rehearsal import rehearse_backup
from milo_home.codex_config import private_create
from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.database import Database
from milo_home.execution_lock import execution_lock
from milo_home.work_runtime import WorkRuntime
from milo_home.workspace_backup import capture_workspace, rehearse_workspace
from milo_home.workspace_git import git

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="Private recovery management requires root"
)


def test_business_journal_and_git_restore_together_without_replaying_unknown_input(
    runtime_case, tmp_path, private_runtime_directory
):
    case, runtime, journal = runtime_case
    work = case[0]
    _, window = reserve(case)
    with execution_lock(work[0]) as lease:
        linked = bind(runtime_case, lease)
        intent = runtime.claim_start(work[1], window["id"], lease=lease)
    journal.record(
        intent.stream_id,
        {
            "kind": "intent",
            "request": {"id": 1, "method": "turn/start", "params": {}},
        },
    )
    journal.record(intent.stream_id, {"kind": "unknown", "id": 1})
    git(tmp_path, "init", "--template=", "--initial-branch=main")
    (tmp_path / ".gitignore").write_text("runtime/\n*.sqlite3*\n")
    (tmp_path / "work.txt").write_text("Synthetic baseline\n")
    git(tmp_path, "add", ".gitignore", "work.txt")
    commit(tmp_path, "Synthetic business-linked workspace")
    (tmp_path / "work.txt").write_text("Synthetic staged result\n")
    git(tmp_path, "add", "work.txt")
    (tmp_path / "work.txt").write_text("Synthetic staged result\nSynthetic later edit\n")
    (tmp_path / "new-result.txt").write_text("Synthetic selected result\n")
    original_database = database_dump(work[0])
    original_evidence = journal.work_snapshot(intent.stream_id)
    root = private_runtime_directory("milo-paired-workspace-")
    captured = capture_workspace(
        tmp_path, root / "git-archive", writers_stopped=True, include=["new-result.txt"]
    )
    business = root / "business.sqlite3"
    report = rehearse_backup(work[0].path, business)
    assert report["active_work_windows_retained"] == 1
    assert report["private_runtime_required"] is True
    restored_home = root / "restored-runtime"
    restored_home.mkdir(mode=0o700)
    private_create(
        restored_home / "config.toml", (journal.path.parent / "config.toml").read_bytes()
    )
    copied_journal = restored_home / journal.path.name
    private_create(copied_journal, b"")
    reader = PrivateJournal(journal.path.parent, initialize=False, read_only=True)
    with reader._connect() as source, sqlite3.connect(copied_journal) as target:
        source.backup(target)
    restored = PrivateJournal(restored_home, initialize=False, read_only=True)
    assert restored.business_binding() == linked
    assert restored.work_snapshot(intent.stream_id) == original_evidence
    assert restored.outstanding()[0]["delivery"] == "unknown"
    git_report = rehearse_workspace(
        root / "git-archive", root / "restored-workspace", captured["manifest_sha256"]
    )
    assert git_report["restore_verified"] and not git_report["automatic_resume"]
    assert (root / "restored-workspace/work.txt").read_bytes() == (
        tmp_path / "work.txt"
    ).read_bytes()
    assert not (root / "restored-workspace/runtime").exists()
    copied_database = Database(business)
    with (
        execution_lock(copied_database) as lease,
        pytest.raises(ProtocolError, match="Existing business main association"),
    ):
        WorkRuntime(copied_database, journal).check_session_binding(
            work[1], work[-1]["id"], lease=lease
        )
    with pytest.raises(ProtocolError, match="installation differs"):
        restored.installation()
    assert database_dump(copied_database) == original_database == database_dump(work[0])
    assert journal.work_snapshot(intent.stream_id) == original_evidence
    assert restored.outstanding() == journal.outstanding()

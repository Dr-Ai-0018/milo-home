import json
import os
import sqlite3

import pytest
from test_backup_rehearsal import command as rehearse_command
from test_database_inspection import command as doctor_command
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_runtime import bind
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case

from milo_home.codex_config import private_create
from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.database import Database
from milo_home.database_inspection import inspect_database
from milo_home.execution_lock import execution_lock
from milo_home.work_runtime import WorkRuntime


def database_dump(database):
    with database.connect() as connection:
        return list(connection.iterdump())


@pytest.mark.parametrize("state", ["held", "running", "stopping", "unknown", "closed"])
def test_work_occupancy_survives_cli_rehearsal_even_after_deadline(execution_case, tmp_path, state):
    work, _, _, _ = execution_case
    database = work[0]
    task, window = reserve(execution_case)
    with database.connect(write=True) as connection:
        connection.execute(
            "UPDATE execution_windows SET state=?,ended_at=? WHERE id=?",
            (state, window["deadline_at"] if state == "closed" else None, window["id"]),
        )
    original = database_dump(database)
    observed = inspect_database(database.path, clock=lambda: window["deadline_at"] + 100)
    expected = int(state != "closed")
    assert len(observed["active_work_windows"]) == expected
    assert observed["active_runs"] == observed["active_main_creations"] == []
    assert observed["private_runtime_required"] is False
    reason = "active_work_window_needs_observation"
    assert (reason in observed["attention"]) == bool(expected)
    if expected:
        assert observed["active_work_windows"][0] == {
            "id": window["id"],
            "task_id": task["id"],
            "state": state,
            "created_at": window["created_at"],
            "deadline_at": window["deadline_at"],
            "deadline_exceeded": 1,
        }
    doctor = doctor_command(database.path, "doctor")
    assert doctor.returncode == 0, doctor.stderr
    assert len(json.loads(doctor.stdout)["active_work_windows"]) == expected
    restored = tmp_path / "rehearsed.sqlite3"
    process = rehearse_command(database.path, restored)
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["active_work_windows_retained"] == expected
    assert report["active_main_creations_retained"] == report["active_runs_retained"] == 0
    assert (reason in report["attention"]) == bool(expected)
    assert report["record_counts"]["execution_windows"] == 1
    assert database_dump(Database(restored)) == original == database_dump(database)
    for secret in [task["initial_input"]["body"], window["unit_name"], str(tmp_path)]:
        assert secret not in doctor.stdout + process.stdout


@pytest.mark.parametrize("state", ["held", "unknown", "closed"])
def test_creation_evidence_is_required_even_without_a_main_binding(work_case, tmp_path, state):
    database, owner, _, workspace, _, _, session = work_case
    with database.connect(write=True) as connection:
        owner_revision = connection.execute(
            "SELECT revision FROM work_access WHERE user_id=?", (owner,)
        ).fetchone()[0]
        workspace_revision = connection.execute(
            "SELECT revision FROM workspaces WHERE id=?", (workspace,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO work_main_creations (id,session_id,choice_digest,owner_revision,"
            "workspace_revision,created_at,deadline_at,state) VALUES (?,?,?,?,?,1,61,?)",
            (
                "synthetic-creation",
                session["id"],
                "a" * 64,
                owner_revision,
                workspace_revision,
                state,
            ),
        )
    before = database_dump(database)
    report = inspect_database(database.path, clock=lambda: 100)
    expected = int(state != "closed")
    assert len(report["active_main_creations"]) == expected
    assert report["active_work_windows"] == []
    assert report["private_runtime_required"] is True
    assert "private_work_runtime_required" in report["attention"]
    assert ("active_main_creation_needs_observation" in report["attention"]) == bool(expected)
    if expected:
        assert report["active_main_creations"][0]["deadline_exceeded"] == 1
    destination = tmp_path / "rehearsed.sqlite3"
    process = rehearse_command(database.path, destination)
    assert process.returncode == 0, process.stderr
    restored = json.loads(process.stdout)
    assert restored["active_main_creations_retained"] == expected
    assert restored["private_runtime_required"] is True
    assert restored["record_counts"]["work_main_creations"] == 1
    assert database_dump(database) == before == database_dump(Database(destination))
    assert "a" * 64 not in json.dumps(report) + process.stdout


def test_work_report_observes_only_committed_wal_in_one_snapshot(execution_case):
    database = execution_case[0][0]
    _, window = reserve(execution_case)
    with database.connect() as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE execution_windows SET state='unknown'")
        assert database.path.with_suffix(".sqlite3-wal").stat().st_size > 0
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE execution_windows SET state='stopping'")
        report = inspect_database(database.path, clock=lambda: window["deadline_at"] + 1)
        assert report["active_work_windows"][0]["state"] == "unknown"
        writer.rollback()
    assert inspect_database(database.path)["active_work_windows"][0]["state"] == "unknown"


@pytest.mark.skipif(os.geteuid() != 0, reason="Shared host lease requires root")
def test_paired_journal_keeps_unknown_delivery_and_copy_cannot_execute(runtime_case, tmp_path):
    case, runtime, journal = runtime_case
    work = case[0]
    _, window = reserve(case)
    with execution_lock(work[0]) as lease:
        linked = bind(runtime_case, lease)
        intent = runtime.claim_start(work[1], window["id"], lease=lease)
    journal.record(
        intent.stream_id,
        {"kind": "intent", "request": {"id": 1, "method": "turn/start", "params": {}}},
    )
    journal.record(intent.stream_id, {"kind": "unknown", "id": 1})
    original = database_dump(work[0])
    restored_database = tmp_path / "restored.sqlite3"
    process = rehearse_command(work[0].path, restored_database)
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["private_runtime_required"] is True
    assert report["active_work_windows_retained"] == 1
    restored_home = tmp_path / "restored-home"
    restored_home.mkdir(mode=0o700)
    private_create(
        restored_home / "config.toml", (journal.path.parent / "config.toml").read_bytes()
    )
    journal_copy = restored_home / journal.path.name
    private_create(journal_copy, b"")
    source_journal = PrivateJournal(journal.path.parent, initialize=False, read_only=True)
    with source_journal._connect() as reader, sqlite3.connect(journal_copy) as writer:
        reader.backup(writer)
    copied = PrivateJournal(restored_home, initialize=False, read_only=True)
    assert copied.business_binding() == linked
    assert copied.fingerprint() == journal.fingerprint()
    assert copied.work_snapshot(intent.stream_id) == journal.work_snapshot(intent.stream_id)
    assert copied.outstanding()[0]["delivery"] == "unknown"
    # New database inode is not the original execution identity, even with the original journal.
    restored = Database(restored_database)
    with (
        execution_lock(restored) as lease,
        pytest.raises(ProtocolError, match="Existing business main association"),
    ):
        WorkRuntime(restored, journal).check_session_binding(work[1], work[-1]["id"], lease=lease)
    # Moving the home also cannot make the saved installation into a new live binding.
    with pytest.raises(ProtocolError, match="installation differs"):
        copied.installation()
    assert database_dump(restored) == original == database_dump(work[0])
    assert copied.outstanding() == journal.outstanding()


@pytest.mark.skipif(os.geteuid() != 0, reason="Shared host lease requires root")
def test_idle_bound_main_still_requires_private_history(runtime_case):
    case, _, _ = runtime_case
    with execution_lock(case[0][0]) as lease:
        bind(runtime_case, lease)
    report = inspect_database(case[0][0].path)
    assert report["active_work_windows"] == report["active_main_creations"] == []
    assert report["private_runtime_required"] is True
    assert "private_work_runtime_required" in report["attention"]

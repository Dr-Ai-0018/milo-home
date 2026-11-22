import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_delivery import delivery_case as delivery_case
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case

from milo_home.codex_rpc import ProtocolError
from milo_home.database import Database
from milo_home.errors import ApiError
from milo_home.execution_lock import execution_lock
from milo_home.work_delivery import WorkDelivery
from milo_home.work_progress import WorkProgress
from milo_home.work_recovery import WorkRecovery

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Host lease requires root")


def public(case, task):
    return WorkProgress(case[0][0]).results(case[0][1], task["id"])["results"]


def test_normal_run_publishes_stable_per_input_results_and_distinct_final_state(
    launch_case, fake_units, monkeypatch
):
    case, runtime, _, launcher, output, _ = launch_case
    task, window = reserve(case)
    case[0][5].append(case[0][1], task["id"], "合成补充", str(uuid4()))
    original = fake_units[0].stop

    def stop(unit):
        confirmed = original(unit)
        case[0][5].stop(case[0][1], task["id"], str(uuid4()))
        return confirmed

    monkeypatch.setattr(fake_units[0], "stop", stop)
    result = launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    assert (result.outcome, result.turns) == ("cancelled", 2)
    results = public(case, task)
    assert len(results) == 2 and all(r["outcome"] == "completed" for r in results)
    assert all(
        set(r) == {"id", "task_id", "input_id", "outcome", "observed_at", "output"} for r in results
    )
    assert all(r["output"]["answer"]["body"] == "合成结果" for r in results)
    assert all(
        r["output"]["actions"] == {"command_calls": 1, "file_change_calls": 0} for r in results
    )
    recovery = WorkRecovery(runtime)
    before = output.read_bytes()
    report = recovery.inspect(case[0][1], window["id"])
    assert report == {
        "task_status": "cancelled",
        "window_state": "closed",
        "private_stream_state": "closed",
        "known_turns": 2,
        "published_turns": 2,
        "unresolved_permits": 0,
        "missing_receipts": 0,
        "recorded_stop": "stop_confirmed",
        "requires_reconciliation": False,
        "publication_pending": False,
    }
    assert recovery.replay_results(case[0][1], window["id"]) == 2
    assert public(case, task) == results and output.read_bytes() == before
    assert len(fake_units[1]) == 1
    with case[0][0].connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM work_events WHERE kind='turn_result'"
            ).fetchone()[0]
            == 2
        )
    for forbidden in [
        "synthetic-secret",
        "synthetic-main",
        "turn-1",
        "stream_id",
        "receipt_sequence",
        "/root",
    ]:
        assert forbidden not in str(results)


def test_publication_failure_keeps_settled_work_and_replays_only_results(launch_case, fake_units):
    case, runtime, journal, launcher, output, _ = launch_case
    task, window = reserve(case)
    with case[0][0].connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER synthetic_publication_failure BEFORE INSERT ON work_events "
            "WHEN NEW.kind='turn_result' BEGIN SELECT RAISE(ABORT,'synthetic publication'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic publication"):
        launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    assert public(case, task) == []
    assert case[0][5].get(case[0][1], task["id"])["status"] == "completed"
    assert not journal.outstanding() and fake_units[1][0].process.poll() is not None
    recovery = WorkRecovery(runtime)
    assert recovery.inspect(case[0][1], window["id"])["known_turns"] == 1
    before = output.read_bytes()
    with case[0][0].connect(write=True) as connection:
        connection.execute("DROP TRIGGER synthetic_publication_failure")
    assert recovery.replay_results(case[0][1], window["id"]) == 1
    assert len(public(case, task)) == 1 and output.read_bytes() == before
    assert len(fake_units[1]) == 1
    with pytest.raises(ApiError):
        launcher.run(case[0][1], window["id"], credential="synthetic-secret")


def test_known_terminal_can_be_replayed_without_clearing_unknown_after_partial_failure(
    launch_case, fake_units, monkeypatch
):
    case, runtime, journal, launcher, output, _ = launch_case
    task, window = reserve(case)
    original = WorkDelivery.wait

    def crash(delivery):
        original(delivery)
        raise OSError("synthetic coordinator death after durable terminal")

    monkeypatch.setattr(WorkDelivery, "wait", crash)
    with pytest.raises(OSError):
        launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    assert public(case, task) == []
    recovery = WorkRecovery(runtime)
    before = output.read_bytes()
    assert recovery.replay_results(case[0][1], window["id"]) == 1
    report = recovery.inspect(case[0][1], window["id"])
    assert report["requires_reconciliation"] and report["window_state"] == "unknown"
    assert report["task_status"] == "needs_reconciliation" and report["known_turns"] == 1
    assert report["published_turns"] == 1 and report["recorded_stop"] == "stop_confirmed"
    assert output.read_bytes() == before and len(fake_units[1]) == 1
    with pytest.raises(ApiError):
        reserve(case)


def test_receipt_write_failure_is_unknown_and_missing_receipt_is_not_invented(
    launch_case, fake_units, monkeypatch
):
    case, runtime, journal, launcher, output, _ = launch_case
    task, window = reserve(case)
    original = journal.record

    def fail(stream, event):
        if event["kind"] == "work_terminal":
            raise OSError("synthetic receipt storage failure")
        return original(stream, event)

    monkeypatch.setattr(journal, "record", fail)
    with pytest.raises(OSError):
        launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    recovery = WorkRecovery(runtime)
    before = output.read_bytes()
    assert recovery.replay_results(case[0][1], window["id"]) == 0
    report = recovery.inspect(case[0][1], window["id"])
    assert report["known_turns"] == 0 and report["missing_receipts"] == 1
    assert report["unresolved_permits"] == 1 and report["requires_reconciliation"]
    assert public(case, task) == [] and output.read_bytes() == before


@pytest.mark.parametrize("change", ["other", "revoke", "regrant", "database-copy"])
def test_result_replay_rechecks_authority_and_database_identity(
    launch_case, fake_units, tmp_path, change
):
    case, runtime, _, launcher, _, _ = launch_case
    _, window = reserve(case)
    launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    owner = case[0][1]
    if change == "other":
        owner = case[0][2]
    elif change in {"revoke", "regrant"}:
        case[0][4].revoke(owner)
        if change == "regrant":
            case[0][4].grant(owner)
    else:
        destination = tmp_path / "copy.sqlite3"
        case[0][0].backup(destination)
        runtime.database = Database(destination)
    recovery = WorkRecovery(runtime)
    for action in [recovery.inspect, recovery.replay_results]:
        with pytest.raises((ApiError, ProtocolError)):
            action(owner, window["id"])


@pytest.mark.parametrize("corruption", ["outcome", "turn", "permit", "extra", "missing-response"])
def test_wrong_private_evidence_is_not_published(delivery_case, corruption):
    case, runtime, journal, _, task, window, open_ = delivery_case
    delivery, _, _ = open_()
    delivery.start(task["initial_input"]["id"])
    delivery.wait()
    # The fixture holds the lease; close it before using the explicit recovery entry.
    delivery.rpc.close()
    assert journal.close_stream(delivery.intent.stream_id, stop=lambda: True)
    with journal._connect() as connection:
        receipt = connection.execute(
            "SELECT * FROM entries WHERE payload LIKE '%work_terminal%'"
        ).fetchone()
        event = json.loads(receipt["payload"])
        if corruption == "outcome":
            event["outcome"] = "failed"
        elif corruption == "turn":
            event["turn_id"] = "another-turn"
        elif corruption == "permit":
            event["work"]["permit_id"] = str(uuid4())
        elif corruption == "extra":
            event["text"] = "private-secret"
        else:
            connection.execute('DELETE FROM entries WHERE payload LIKE \'%"kind": "response"%\'')
        connection.execute(
            "UPDATE entries SET payload=? WHERE sequence=?",
            (json.dumps(event), receipt["sequence"]),
        )
    # Inspect the lower-level locked reader with the fixture's valid original lease.
    from milo_home.work_recovery import _read_evidence

    with pytest.raises(ProtocolError):
        _read_evidence(runtime, case[0][1], window["id"], delivery.lease)
    assert public(case, task) == []


def test_mismatched_private_binding_does_not_rebind(runtime_case):
    from test_work_runtime import bind

    case, runtime, journal = runtime_case
    _, window = reserve(case)
    with execution_lock(case[0][0]) as lease:
        bind(runtime_case, lease)
        runtime.claim_start(case[0][1], window["id"], lease=lease)
    original = journal.business_binding()
    with journal._connect() as connection:
        connection.execute(
            "UPDATE business_binding SET value=?",
            (json.dumps(replace(original, owner_id=str(uuid4())).__dict__),),
        )
    with pytest.raises(ProtocolError):
        WorkRecovery(runtime).inspect(case[0][1], window["id"])


def test_result_scope_and_immutability_are_enforced_by_sql(launch_case, fake_units):
    case, _, _, launcher, _, _ = launch_case
    _, window = reserve(case)
    launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    with case[0][0].connect(write=True) as connection:
        for statement in ["DELETE FROM work_results", "UPDATE work_results SET outcome='failed'"]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
        row = dict(connection.execute("SELECT * FROM work_results").fetchone())
        row.update(id=str(uuid4()), input_id=str(uuid4()), permit_id=str(uuid4()))
        with pytest.raises(sqlite3.IntegrityError, match="scope mismatch"):
            connection.execute(
                "INSERT INTO work_results VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(row.values())
            )


def test_publisher_process_death_rolls_back_and_reopen_only_publishes_known_results(
    launch_case, fake_units
):
    case, runtime, journal, launcher, output, _ = launch_case
    task, window = reserve(case)
    case[0][5].append(case[0][1], task["id"], "第二个合成结果", str(uuid4()))
    with case[0][0].connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER synthetic_block BEFORE INSERT ON work_results "
            "BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    with case[0][0].connect(write=True) as connection:
        connection.execute("DROP TRIGGER synthetic_block")
    protocol_before = output.read_bytes()
    program = """
import os,sys
from contextlib import contextmanager
from pathlib import Path
from milo_home.database import Database
from milo_home.codex_journal import PrivateJournal
from milo_home.work_runtime import WorkRuntime
from milo_home.work_recovery import WorkRecovery
class CrashDatabase(Database):
    @contextmanager
    def connect(self, *, write=False):
        with super().connect(write=write) as connection:
            if write:
                seen = [0]
                def trace(statement):
                    if statement.startswith('INSERT INTO work_results'):
                        seen[0] += 1
                        # SQLite repeats the traced statement for triggers too.
                        # Exit after one full result/event pair has been inserted.
                        if connection.execute('SELECT count(*) FROM work_results').fetchone()[0]:
                            os._exit(93)
                connection.set_trace_callback(trace)
            yield connection
runtime = WorkRuntime(CrashDatabase(Path(sys.argv[1])), PrivateJournal(Path(sys.argv[2])))
WorkRecovery(runtime).replay_results(sys.argv[3], sys.argv[4])
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(case[0][0].path),
            str(journal.path.parent),
            case[0][1],
            window["id"],
        ],
        timeout=5,
    )
    assert child.returncode == 93
    assert public(case, task) == []
    with case[0][0].connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM work_events WHERE kind='turn_result'"
            ).fetchone()[0]
            == 0
        )
    from milo_home.codex_journal import PrivateJournal
    from milo_home.work_runtime import WorkRuntime

    reopened = WorkRuntime(Database(case[0][0].path), PrivateJournal(journal.path.parent))
    recovery = WorkRecovery(reopened)
    assert recovery.inspect(case[0][1], window["id"])["publication_pending"]
    assert recovery.replay_results(case[0][1], window["id"]) == 2
    assert recovery.replay_results(case[0][1], window["id"]) == 2
    assert len(public(case, task)) == 2 and output.read_bytes() == protocol_before
    assert len(fake_units[1]) == 1

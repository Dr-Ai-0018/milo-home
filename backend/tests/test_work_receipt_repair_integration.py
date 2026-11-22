import json
import os
import sqlite3
import subprocess
import sys

import pytest
from test_execution_control import execution_case as execution_case
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_reconciliation import abandon
from test_work_reconciliation import fresh_stop as fresh_stop
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case

from milo_home.codex_rpc import ProtocolError
from milo_home.errors import ApiError
from milo_home.execution_budget import ExecutionBudget
from milo_home.work_progress import WorkProgress
from milo_home.work_reconciliation import WorkReconciliation
from milo_home.work_recovery import WorkRecovery

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root lease")


def entries(journal):
    with journal._connect() as connection:
        return [json.loads(r[0]) for r in connection.execute("SELECT payload FROM entries")]


def test_repair_requires_matching_native_failure_and_keeps_launch_charge(
    launch_case, fake_units, fresh_stop, monkeypatch
):
    case, runtime, journal, _, output, _ = launch_case
    task, window = abandon(launch_case, monkeypatch, mode="missing")
    binding = journal.binding()
    with journal._connect() as connection:
        sequence, stream, payload = connection.execute(
            "SELECT sequence,stream,payload FROM entries WHERE payload LIKE '%turn/completed%'"
        ).fetchone()
        turn = json.loads(payload)["message"]["params"]["turn"]["id"]
        error = {
            "kind": "notification",
            "message": {
                "method": "error",
                "params": {
                    "threadId": binding.thread_id,
                    "turnId": turn,
                    "willRetry": False,
                    "error": {"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 503}}},
                },
            },
        }
        connection.execute(
            "UPDATE entries SET payload=? WHERE sequence=? AND stream=?",
            (json.dumps(error), sequence, stream),
        )
    name = "sessions/2026/11/13/rollout-synthetic.jsonl"
    file = journal.path.parent / name
    file.parent.mkdir(parents=True)
    meta = {"type": "session_meta", "payload": {"id": binding.thread_id}}
    terminal = {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": turn,
            "last_agent_message": None,
            "error": {"codex_error_info": {"http_connection_failed": {"http_status_code": 502}}},
        },
    }

    def write_rollout():
        file.write_text(json.dumps(meta) + "\n" + json.dumps(terminal) + "\n")

    write_rollout()
    recovery = WorkReconciliation(runtime)
    kwargs = {"repair_receipts": True, "native_rollout": name}
    assert recovery.settle(case[0][1], window["id"], **kwargs)["blocked_by"] == ["missing_receipts"]
    before = output.read_bytes()
    budget = ExecutionBudget(runtime.database, runtime.clock)
    used = budget.view()["held_or_charged_launches"]
    info = terminal["payload"]["error"]["codex_error_info"]
    info["http_connection_failed"]["http_status_code"] = 503
    write_rollout()
    # Settling after the deadline preserves the original failure.
    case[-1][0] += 120
    assert recovery.settle(case[0][1], window["id"], **kwargs)["outcome"] == "failed"
    assert budget.view()["held_or_charged_launches"] == used == 1
    assert len(fake_units[1]) == 1 and output.read_bytes() == before
    results = WorkProgress(runtime.database).results(case[0][1], task["id"])["results"]
    assert results[0]["outcome"] == "failed" and results[0]["output"]["answer"]["body"] is None
    assert not WorkRecovery(runtime).inspect(case[0][1], window["id"])["requires_reconciliation"]


def test_explicit_repair_recovers_facts_without_output_or_another_launch(
    launch_case, fake_units, fresh_stop, monkeypatch
):
    case, runtime, journal, _, output, _ = launch_case
    task, window = abandon(launch_case, monkeypatch, mode="missing")
    before = output.read_bytes()
    original = entries(journal)
    assert WorkRecovery(runtime).replay_results(case[0][1], window["id"]) == 0
    recovery = WorkReconciliation(runtime)
    assert recovery.settle(case[0][1], window["id"])["blocked_by"] == ["missing_receipts"]
    assert recovery.settle(case[0][1], window["id"], repair_receipts=True)["closed"]
    results = WorkProgress(case[0][0]).results(case[0][1], task["id"])["results"]
    assert len(results) == 1 and results[0]["outcome"] == "completed"
    assert results[0]["output"] == {
        "answer": {"state": "missing", "body": None, "reason": "not_recorded"},
        "actions": None,
    }
    repaired = entries(journal)
    assert repaired[: len(original)] == original
    assert [e["kind"] for e in repaired[-3:]] == [
        "work_terminal",
        "work_receipt_repair",
        "work_stop",
    ]
    assert repaired[-2]["source"] == "original_protocol"
    assert recovery.settle(case[0][1], window["id"], repair_receipts=True)["closed"]
    assert entries(journal) == repaired and len(fresh_stop) == 2
    assert len(fake_units[1]) == 1 and output.read_bytes() == before


@pytest.mark.parametrize("scenario", ["missing-terminal", "unknown-response", "stop", "revoke"])
def test_incomplete_evidence_or_failed_controls_remain_occupied(
    launch_case, fake_units, fresh_stop, monkeypatch, scenario
):
    case, runtime, journal, _, _, _ = launch_case
    task, window = abandon(
        launch_case, monkeypatch, mode="response" if scenario == "unknown-response" else "missing"
    )
    if scenario == "missing-terminal":
        with journal._connect() as connection:
            connection.execute("DELETE FROM entries WHERE payload LIKE '%turn/completed%'")
    elif scenario == "stop":
        monkeypatch.setattr("milo_home.work_reconciliation.stop_family", lambda _: False)
    elif scenario == "revoke":
        case[0][4].revoke(case[0][1])
    recovery = WorkReconciliation(runtime)
    if scenario == "revoke":
        with pytest.raises(ApiError):
            recovery.settle(case[0][1], window["id"], repair_receipts=True)
    else:
        report = recovery.settle(case[0][1], window["id"], repair_receipts=True)
        assert not report["closed"]
    assert not any(e["kind"] == "work_receipt_repair" for e in entries(journal))
    with case[0][0].connect() as connection:
        assert connection.execute("SELECT state FROM execution_windows").fetchone()[0] == "unknown"
        assert connection.execute("SELECT count(*) FROM work_results").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["private", "business", "retention"])
def test_atomic_repair_and_reopen_after_storage_failure(
    launch_case, fake_units, fresh_stop, monkeypatch, failure
):
    case, runtime, journal, _, _, _ = launch_case
    _, window = abandon(launch_case, monkeypatch, mode="missing")
    original = entries(journal)
    with journal._connect() if failure != "business" else case[0][0].connect(write=True) as conn:
        if failure == "retention":
            conn.execute("UPDATE streams SET bytes=?", (8 * 1024 * 1024,))
        elif failure == "private":
            conn.execute(
                "CREATE TRIGGER synthetic_repair_failure BEFORE INSERT ON entries "
                "WHEN NEW.payload LIKE '%work_receipt_repair%' "
                "BEGIN SELECT RAISE(ABORT,'synthetic repair'); END"
            )
        else:
            conn.execute(
                "CREATE TRIGGER synthetic_repair_failure BEFORE INSERT ON work_results "
                "BEGIN SELECT RAISE(ABORT,'synthetic repair'); END"
            )
    recovery = WorkReconciliation(runtime)
    with pytest.raises((sqlite3.IntegrityError, ProtocolError)):
        recovery.settle(case[0][1], window["id"], repair_receipts=True)
    assert len(fresh_stop) == 1
    if failure != "business":
        assert entries(journal) == original
    with journal._connect() if failure != "business" else case[0][0].connect(write=True) as conn:
        if failure == "retention":
            conn.execute("UPDATE streams SET bytes=0")
        else:
            conn.execute("DROP TRIGGER synthetic_repair_failure")
    assert recovery.settle(case[0][1], window["id"], repair_receipts=True)["closed"]
    assert len(fresh_stop) == 2
    assert sum(e["kind"] == "work_receipt_repair" for e in entries(journal)) == 1


@pytest.mark.parametrize(
    "terminal,outcome",
    [("normal", "completed"), ("failed-terminal", "failed"), ("interrupted-terminal", "cancelled")],
)
def test_repaired_terminal_preserves_unsuccessful_outcomes(
    launch_case, fake_units, fresh_stop, monkeypatch, terminal, outcome
):
    case, runtime, _, _, _, selected = launch_case
    selected[0] = terminal
    _, window = abandon(launch_case, monkeypatch, mode="missing")
    report = WorkReconciliation(runtime).settle(case[0][1], window["id"], repair_receipts=True)
    assert report["closed"] and report["outcome"] == outcome


def test_existing_output_is_kept_when_explicit_repair_finds_no_missing_receipt(
    launch_case, fake_units, fresh_stop, monkeypatch
):
    case, runtime, journal, _, _, _ = launch_case
    task, window = abandon(launch_case, monkeypatch)
    assert WorkReconciliation(runtime).settle(case[0][1], window["id"], repair_receipts=True)[
        "closed"
    ]
    result = WorkProgress(case[0][0]).results(case[0][1], task["id"])["results"][0]
    assert result["output"]["answer"]["body"] == "合成结果"
    assert not any(e["kind"] == "work_receipt_repair" for e in entries(journal))


@pytest.mark.parametrize("race", ["private-entry", "request", "revoke", "permit"])
def test_changes_before_private_repair_are_rejected_without_recovered_receipt(
    launch_case, fake_units, fresh_stop, monkeypatch, race
):
    case, runtime, journal, _, _, _ = launch_case
    _, window = abandon(launch_case, monkeypatch, mode="missing")
    from milo_home import work_receipt_repair as module

    original = module.check_receipt_permits

    def change(connection, window_id, receipts):
        if race == "permit":
            raise ProtocolError("Work receipt differs from its launched permit")
        original(connection, window_id, receipts)
        if race == "revoke":
            return
        with journal._connect() as private:
            if race == "private-entry":
                private.execute(
                    "INSERT INTO entries (stream,payload) VALUES (?,?)",
                    (receipts[0][1]["work"]["stream_id"], '{"kind":"synthetic_change"}'),
                )
            else:
                private.execute("UPDATE requests SET state='unknown' WHERE method='turn/start'")

    monkeypatch.setattr(module, "check_receipt_permits", change)
    if race == "revoke":

        def stop(_):
            case[0][4].revoke(case[0][1])
            return True

        monkeypatch.setattr("milo_home.work_reconciliation.stop_family", stop)
    with pytest.raises((ProtocolError, ApiError)):
        WorkReconciliation(runtime).settle(case[0][1], window["id"], repair_receipts=True)
    assert not any(e["kind"] == "work_terminal" for e in entries(journal))
    with case[0][0].connect() as connection:
        assert connection.execute("SELECT state FROM execution_windows").fetchone()[0] == "unknown"


def test_process_death_during_private_repair_rolls_back_the_whole_batch(
    launch_case, fake_units, fresh_stop, monkeypatch
):
    case, runtime, journal, _, output, _ = launch_case
    _, window = abandon(launch_case, monkeypatch, mode="missing")
    original = entries(journal)
    protocol = output.read_bytes()
    program = """
import os,sys
from pathlib import Path
from contextlib import contextmanager
from milo_home.database import Database
from milo_home.codex_journal import PrivateJournal
from milo_home.work_runtime import WorkRuntime
from milo_home.work_reconciliation import WorkReconciliation
import milo_home.work_reconciliation as module
module.stop_family = lambda identity: True
class CrashJournal(PrivateJournal):
    @contextmanager
    def _connect(self):
        with super()._connect() as connection:
            def trace(statement):
                if (statement.startswith('INSERT INTO entries')
                        and 'work_receipt_repair' in statement):
                    os._exit(95)
            connection.set_trace_callback(trace)
            yield connection
runtime = WorkRuntime(Database(Path(sys.argv[1])), CrashJournal(Path(sys.argv[2])),
                      lambda: int(sys.argv[5]))
WorkReconciliation(runtime).settle(sys.argv[3],sys.argv[4],repair_receipts=True)
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
            str(case[-1][0]),
        ],
        timeout=6,
    )
    assert child.returncode == 95 and entries(journal) == original
    assert WorkReconciliation(runtime).settle(case[0][1], window["id"], repair_receipts=True)[
        "closed"
    ]
    assert len(fresh_stop) == 1 and output.read_bytes() == protocol

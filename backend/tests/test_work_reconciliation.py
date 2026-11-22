import json
import os
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case

from milo_home.codex_rpc import DeliveryUnknown
from milo_home.errors import ApiError
from milo_home.execution_budget import ExecutionBudget
from milo_home.work_progress import WorkProgress
from milo_home.work_reconciliation import WorkReconciliation
from milo_home.work_recovery import WorkRecovery

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root lease")


def abandon(launch_case, monkeypatch, *, mode="receipt", supplement=False):
    case, _, journal, launcher, _, selected = launch_case
    task, window = reserve(case)
    if supplement:
        case[0][5].append(case[0][1], task["id"], "合成未投递补充", str(uuid4()))
    original = journal.record

    def fail(stream, event):
        if event["kind"] == "work_terminal":
            if mode != "missing":
                original(stream, event)
            raise OSError("synthetic crash at receipt boundary")
        return original(stream, event)

    if mode == "response":
        selected[0] = "eof"
    with monkeypatch.context() as patch:
        if mode != "response":
            patch.setattr(journal, "record", fail)
        with pytest.raises((OSError, DeliveryUnknown)):
            launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    assert case[0][5].get(case[0][1], task["id"])["status"] == "needs_reconciliation"
    return task, window


@pytest.fixture
def fresh_stop(monkeypatch):
    calls = []

    def stop(identity):
        assert os.geteuid() == 0
        calls.append(identity)
        return True

    monkeypatch.setattr("milo_home.work_reconciliation.stop_family", stop)
    return calls


def states(case):
    with case[0][0].connect() as connection:
        return {
            "window": connection.execute("SELECT state FROM execution_windows").fetchone()[0],
            "permits": [r[0] for r in connection.execute("SELECT state FROM execution_permits")],
            "audits": connection.execute("SELECT count(*) FROM work_reconciliations").fetchone()[0],
        }


def test_known_receipt_closes_unknown_once_and_preserves_original_result(
    launch_case, fake_units, monkeypatch, fresh_stop
):
    case, runtime, journal, launcher, output, _ = launch_case
    task, window = abandon(launch_case, monkeypatch)
    assert states(case) == {"window": "unknown", "permits": ["unknown"], "audits": 0}
    before = output.read_bytes()
    budget_before = ExecutionBudget(case[0][0], lambda: case[-1][0]).view()
    recovery = WorkReconciliation(runtime)
    assert recovery.settle(case[0][1], window["id"]) == {
        "closed": True,
        "outcome": "completed",
        "blocked_by": [],
    }
    assert states(case) == {"window": "closed", "permits": ["finished"], "audits": 1}
    result = WorkProgress(case[0][0]).results(case[0][1], task["id"])["results"][0]
    assert result["output"]["answer"]["body"] == "合成结果"
    assert not WorkRecovery(runtime).inspect(case[0][1], window["id"])["requires_reconciliation"]
    assert recovery.settle(case[0][1], window["id"])["closed"]
    assert len(fresh_stop) == 1 and output.read_bytes() == before and len(fake_units[1]) == 1
    assert not journal.outstanding()
    budget_after = ExecutionBudget(case[0][0], lambda: case[-1][0]).view()
    assert (
        budget_before["held_or_charged_launches"] == budget_after["held_or_charged_launches"] == 1
    )
    with case[0][0].connect(write=True) as connection:
        for sql in [
            "DELETE FROM work_reconciliations",
            "UPDATE work_reconciliations SET stop_sequence=1",
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
    with pytest.raises(ApiError):
        launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    # A new task can use the released occupancy; the original one cannot reopen.
    reserve(case)


@pytest.mark.parametrize(
    "condition,outcome",
    [
        ("supplement", "failed"),
        ("stop", "cancelled"),
        ("deadline", "timed_out"),
        ("pause", "cancelled"),
    ],
)
def test_recovery_does_not_claim_unprocessed_work_was_completed(
    launch_case, fake_units, monkeypatch, fresh_stop, condition, outcome
):
    case, runtime, _, _, output, _ = launch_case
    task, window = abandon(launch_case, monkeypatch, supplement=condition == "supplement")
    if condition == "stop":
        case[0][5].stop(case[0][1], task["id"], str(uuid4()))
    elif condition == "deadline":
        case[-1][0] = window["deadline_at"]
    elif condition == "pause":
        case[2].pause(True)
    before = output.read_bytes()
    assert WorkReconciliation(runtime).settle(case[0][1], window["id"])["outcome"] == outcome
    assert output.read_bytes() == before
    if condition == "supplement":
        inputs = case[0][5].inputs(case[0][1], task["id"], 0, 20)["inputs"]
        assert [i["delivery"] for i in inputs] == ["acknowledged", "cancelled"]


@pytest.mark.parametrize(
    "mode,reason", [("missing", "missing_receipts"), ("response", "pending_requests")]
)
def test_fresh_stop_does_not_repair_missing_terminal_or_unknown_delivery(
    launch_case, fake_units, monkeypatch, fresh_stop, mode, reason
):
    case, runtime, journal, _, output, _ = launch_case
    task, window = abandon(launch_case, monkeypatch, mode=mode)
    before = output.read_bytes()
    report = WorkReconciliation(runtime).settle(case[0][1], window["id"])
    assert report == {"closed": False, "outcome": "needs_reconciliation", "blocked_by": [reason]}
    assert states(case)["window"] == "unknown" and states(case)["audits"] == 0
    assert states(case)["permits"] == ["unknown"]
    assert WorkProgress(case[0][0]).results(case[0][1], task["id"]) == {"results": []}
    assert len(fresh_stop) == 1 and output.read_bytes() == before
    with pytest.raises(ApiError):
        reserve(case)
    # The new stop is retained even though the stream/task cannot be cleared.
    stream = next(r["id"] for r in journal.outstanding()) if journal.outstanding() else None
    with journal._connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM entries WHERE payload LIKE '%work_stop%'"
            ).fetchone()[0]
            == 1
        )
    assert mode != "response" or stream is not None


def test_old_ready_or_stale_stop_never_substitutes_for_fresh_identity(
    launch_case, fake_units, monkeypatch, fresh_stop
):
    case, runtime, journal, _, _, _ = launch_case
    _, window = abandon(launch_case, monkeypatch)
    with journal._connect() as connection:
        row = connection.execute(
            'SELECT * FROM entries WHERE payload LIKE \'%"phase": "ready"%\''
        ).fetchone()
        payload = json.loads(row["payload"])
        del payload["group"]
        connection.execute(
            "UPDATE entries SET payload=? WHERE sequence=?", (json.dumps(payload), row["sequence"])
        )
    report = WorkReconciliation(runtime).settle(case[0][1], window["id"])
    assert report["blocked_by"] == ["missing_process_identity"] and not fresh_stop
    assert states(case)["window"] == "unknown"


@pytest.mark.parametrize("failure", ["stop", "clock", "revoke", "event", "private"])
def test_failure_never_releases_occupancy_and_retries_need_new_stop(
    launch_case, fake_units, monkeypatch, fresh_stop, failure
):
    case, runtime, journal, _, _, _ = launch_case
    task, window = abandon(launch_case, monkeypatch)
    recovery = WorkReconciliation(runtime)
    with monkeypatch.context() as patch:
        if failure == "stop":
            patch.setattr("milo_home.work_reconciliation.stop_family", lambda _: False)
        elif failure == "clock":
            case[-1][0] -= 1
        elif failure == "revoke":

            def revoke(identity):
                case[0][4].revoke(case[0][1])
                return True

            patch.setattr("milo_home.work_reconciliation.stop_family", revoke)
        elif failure == "event":
            with case[0][0].connect(write=True) as connection:
                connection.execute(
                    "CREATE TRIGGER synthetic_reconcile_failure BEFORE INSERT ON work_events "
                    "WHEN NEW.state='completed' BEGIN SELECT RAISE(ABORT,'synthetic'); END"
                )
        else:
            patch.setattr(
                journal,
                "reconcile_stream",
                lambda *args: (_ for _ in ()).throw(OSError("synthetic")),
            )
        if failure in {"private", "event", "revoke"}:
            with pytest.raises((OSError, sqlite3.IntegrityError, ApiError)):
                recovery.settle(case[0][1], window["id"])
        else:
            report = recovery.settle(case[0][1], window["id"])
            assert not report["closed"]
    assert states(case) == {"window": "unknown", "permits": ["unknown"], "audits": 0}
    if failure == "revoke":
        return
    assert WorkProgress(case[0][0]).results(case[0][1], task["id"])["results"] == []
    if failure == "clock":
        case[-1][0] += 1
    if failure == "event":
        with case[0][0].connect(write=True) as connection:
            connection.execute("DROP TRIGGER synthetic_reconcile_failure")
    before = len(fresh_stop)
    assert recovery.settle(case[0][1], window["id"])["closed"]
    assert len(fresh_stop) == before + 1


def test_sql_audit_and_permit_transition_cannot_clear_unknown_without_results(
    launch_case, fake_units, monkeypatch
):
    case, _, _, _, _, _ = launch_case
    _, window = abandon(launch_case, monkeypatch)
    with case[0][0].connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="cannot replay"):
            connection.execute(
                "UPDATE execution_permits SET state='finished',ended_at=?", (case[-1][0],)
            )
        stream = connection.execute("SELECT stream_id FROM execution_start_intents").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="scope mismatch"):
            connection.execute(
                "INSERT INTO work_reconciliations VALUES (?,?,?,?,?)",
                (window["id"], stream, 1, case[-1][0], "completed"),
            )


@pytest.mark.parametrize(
    "terminal,outcome", [("failed-terminal", "failed"), ("interrupted-terminal", "cancelled")]
)
def test_known_unsuccessful_turn_never_becomes_completed(
    launch_case, fake_units, monkeypatch, fresh_stop, terminal, outcome
):
    case, runtime, _, _, _, selected = launch_case
    selected[0] = terminal
    _, window = abandon(launch_case, monkeypatch)
    assert WorkReconciliation(runtime).settle(case[0][1], window["id"])["outcome"] == outcome


@pytest.mark.skipif(not os.path.exists("/run/systemd/system"), reason="Requires systemd")
def test_real_recovery_rechecks_collected_systemd_unit(launch_case, monkeypatch):
    case, runtime, _, _, output, _ = launch_case
    task, window = abandon(launch_case, monkeypatch)
    before = output.read_bytes()
    assert WorkReconciliation(runtime).settle(case[0][1], window["id"])["closed"]
    assert output.read_bytes() == before
    assert (
        WorkProgress(case[0][0]).results(case[0][1], task["id"])["results"][0]["outcome"]
        == "completed"
    )


def test_recovery_process_death_rolls_back_audit_and_requires_new_stop(
    launch_case, fake_units, monkeypatch, fresh_stop
):
    case, runtime, journal, _, output, _ = launch_case
    task, window = abandon(launch_case, monkeypatch)
    before = output.read_bytes()
    program = """
import os,sys
from contextlib import contextmanager
from pathlib import Path
from milo_home.database import Database
from milo_home.codex_journal import PrivateJournal
from milo_home.work_runtime import WorkRuntime
from milo_home.work_reconciliation import WorkReconciliation
import milo_home.work_reconciliation as module
module.stop_family = lambda identity: True
class CrashDatabase(Database):
    @contextmanager
    def connect(self, *, write=False):
        with super().connect(write=write) as connection:
            if write:
                def trace(statement):
                    if statement.startswith("UPDATE execution_windows SET state='closed'"):
                        os._exit(94)
                connection.set_trace_callback(trace)
            yield connection
runtime = WorkRuntime(CrashDatabase(Path(sys.argv[1])), PrivateJournal(Path(sys.argv[2])),
                      lambda: int(sys.argv[5]))
WorkReconciliation(runtime).settle(sys.argv[3], sys.argv[4])
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
    assert child.returncode == 94
    assert states(case) == {"window": "unknown", "permits": ["unknown"], "audits": 0}
    assert WorkProgress(case[0][0]).results(case[0][1], task["id"])["results"] == []
    assert WorkReconciliation(runtime).settle(case[0][1], window["id"])["closed"]
    assert len(fresh_stop) == 1 and output.read_bytes() == before
    with journal._connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM entries WHERE payload LIKE '%work_stop%'"
            ).fetchone()[0]
            == 2
        )

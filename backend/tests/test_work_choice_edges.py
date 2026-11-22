"""Bounded edge checks for new results, explicit capability and real stop precedence."""

import json
import sys
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_native_reply_execution import native_reply_case as native_reply_case
from test_reply_readiness import preparation as preparation
from test_work_choice_native import setup_work, starts
from test_work_cli import runtime_case as runtime_case
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_receipt_repair import evidence as evidence
from test_work_revisit_runtime import prepare_revisit, report, tick
from test_work_tasks import work_case as work_case

from milo_home.cli import main
from milo_home.codex_rpc import ProtocolError
from milo_home.errors import ApiError
from milo_home.scheduler_cli import StopRequest
from milo_home.work_decision_native import WORK_CHOICE_SCHEMA
from milo_home.work_delivery import WorkDelivery
from milo_home.work_receipt_repair import repair_candidates
from milo_home.work_receiver import receive_once
from milo_home.work_reconciliation import WorkReconciliation
from milo_home.work_recovery import WorkRecovery


@pytest.mark.parametrize("scenario", ["auto-work-invalid", "auto-work-sensitive"])
def test_invalid_or_protected_work_result_never_becomes_a_personal_decision(
    native_reply_case, scenario
):
    database, owner, runtime, launcher, tasks, task = setup_work(native_reply_case, scenario)
    with pytest.raises(ProtocolError):
        launcher.run_task(owner, task["id"], 60, credential="synthetic-private-file-key")
    assert tasks.get(owner, task["id"])["status"] == "needs_reconciliation"
    assert tasks.get(owner, task["id"])["decision"] is None
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM work_choice_receipts").fetchone()[0] == 0
        assert connection.execute("SELECT state FROM execution_windows").fetchone()[0] == "unknown"
    assert len(starts(native_reply_case)) == 1
    assert not list(runtime.journal.path.parent.glob("*.env"))


def test_redefer_creates_one_new_future_intent_and_does_not_loop_ticks(native_reply_case):
    result, _, _ = prepare_revisit(native_reply_case)
    database, owner, runtime, _, tasks, task = result
    native_reply_case[3][0] = "auto-work-deferred"
    assert tick(result).outcome == "completed"
    assert tick(result).outcome == "queue_empty"
    with database.snapshot() as connection:
        rows = connection.execute(
            "SELECT state FROM work_revisits ORDER BY created_at,id"
        ).fetchall()
        assert [row[0] for row in rows] == ["completed", "scheduled"]
    assert tasks.get(owner, task["id"])["decision"]["state"] == "deferred"
    native_reply_case[0][1][0] += 61
    native_reply_case[3][0] = "auto-work-accepted"
    assert tick(result).outcome == "completed"
    assert tick(result).outcome == "queue_empty"
    assert len(starts(native_reply_case)) == 3


def test_stopping_an_accepted_return_wins_over_its_successful_native_choice(
    native_reply_case, monkeypatch
):
    result, _, _ = prepare_revisit(native_reply_case)
    database, owner, _, _, tasks, task = result
    acknowledge = WorkDelivery._acknowledge

    def stop(delivery):
        acknowledge(delivery)
        tasks.stop(owner, task["id"], str(uuid4()))

    monkeypatch.setattr(WorkDelivery, "_acknowledge", stop)
    with pytest.raises(ApiError, match="execution_window_closed"):
        tick(result)
    with database.snapshot() as connection:
        window = connection.execute(
            "SELECT id FROM execution_windows WHERE state='unknown'"
        ).fetchone()[0]
    assert tasks.get(owner, task["id"])["status"] == "needs_reconciliation"
    WorkRecovery(result[2]).replay_results(owner, window)
    monkeypatch.setattr("milo_home.work_reconciliation.stop_family", lambda _: True)
    assert WorkReconciliation(result[2]).settle(owner, window)["outcome"] == "cancelled"
    saved = tasks.get(owner, task["id"])
    assert saved["status"] == "cancelled" and saved["decision"] is None
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM work_decisions").fetchone()[0] == 1
        assert connection.execute("SELECT state FROM work_revisits").fetchone()[0] == "cancelled"
    assert len(starts(native_reply_case)) == 2


def test_missing_launch_basis_is_not_reconstructed_by_historical_admission(native_reply_case):
    result, window, _ = prepare_revisit(native_reply_case)
    assert report(result, window)["eligible"]
    with result[0].connect(write=True) as connection:
        connection.execute("DELETE FROM work_turn_bases")
    assert report(result, window)["blocked_by"] == ["work_choice_projection_incomplete"]


def test_new_native_choice_cannot_be_guessed_by_legacy_fact_repair(evidence):
    entries, scope = evidence
    entries[0][1]["request"]["params"]["outputSchema"] = WORK_CHOICE_SCHEMA
    with pytest.raises(ProtocolError):
        repair_candidates(entries, **scope, observed_at=100)


def test_cli_work_capability_is_explicit_preserved_and_legacy_runtime_refuses(
    launch_case, monkeypatch, capsys
):
    case, runtime, journal, launcher, _, _ = launch_case
    database, owner, _, _, _, _, session = case[0]
    monkeypatch.setenv("MILO_DATABASE", str(database.path))
    base = [
        "milo-home",
        "--run-directory",
        str(journal.path.parent),
        "automatic-service",
        "register",
        "--kind",
        "work",
        "--owner",
        owner,
        "--session",
        session["id"],
        "--timer-unit",
        "milo-home-work.timer",
        "--enable",
    ]
    monkeypatch.setattr(sys, "argv", [*base, "--work-decisions"])
    main()
    assert json.loads(capsys.readouterr().out)["work_decisions_enabled"] is True
    monkeypatch.setattr(sys, "argv", base)
    main()
    assert json.loads(capsys.readouterr().out)["work_decisions_enabled"] is True
    with pytest.raises(ProtocolError, match="original native main"):
        receive_once(
            launcher, StopRequest(), owner, session["id"], 60, credential="synthetic-secret"
        )
    monkeypatch.setattr(sys, "argv", [*base, "--no-work-decisions"])
    main()
    assert json.loads(capsys.readouterr().out)["work_decisions_enabled"] is False
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM execution_windows").fetchone()[0] == 0

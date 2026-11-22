"""Sourced scheduled work resumes through the actual receiver, not a fabricated visitor post."""

import json
from uuid import uuid4

import pytest
from test_native_reply_execution import native_reply_case as native_reply_case
from test_reply_readiness import preparation as preparation
from test_work_choice_native import setup_work, starts
from test_work_process import fake_units as fake_units

from milo_home.automatic_services import AutomaticServices
from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.execution_budget import ExecutionBudget, ExecutionPolicy
from milo_home.execution_lock import execution_lock
from milo_home.journal_retention_scope import inspect_stream
from milo_home.reception import input_cursor, save
from milo_home.scheduler_cli import StopRequest
from milo_home.work_process import WorkLauncher
from milo_home.work_receiver import receive_once
from milo_home.work_reconciliation import WorkReconciliation
from milo_home.work_recovery import WorkRecovery
from milo_home.work_runtime import WorkRuntime


def prepare_revisit(native_reply_case, *, pending=False):
    result = setup_work(native_reply_case, "auto-work-deferred")
    database, owner, runtime, launcher, tasks, task = result
    if pending:
        tasks.append(owner, task["id"], "合成开始前尚未递到的补充", str(uuid4()))
    window, _ = launcher.run_task(owner, task["id"], 60, credential="synthetic-private-file-key")
    services = AutomaticServices(database, runtime.clock)
    services.configure(
        "work",
        owner,
        "milo-home-work.timer",
        session=task["session_id"],
        enabled=True,
        work_decisions=True,
    )
    services.observe("work", owner, timer_active=True)
    native_reply_case[0][1][0] += 61
    native_reply_case[3][0] = "auto-work-accepted"
    return result, window, services


def tick(result):
    _, owner, _, launcher, _, task = result
    return receive_once(
        launcher,
        StopRequest(),
        owner,
        task["session_id"],
        60,
        credential="synthetic-private-file-key",
    )


def report(result, window):
    database, _, runtime, _, _, _ = result
    with database.snapshot() as connection:
        stream = connection.execute(
            "SELECT stream_id FROM execution_start_intents WHERE window_id=?", (window,)
        ).fetchone()[0]
    with execution_lock(database) as lease:
        return inspect_stream(runtime, stream, lease=lease)


@pytest.mark.parametrize("pending", [False, True])
def test_due_revisit_runs_once_with_or_without_a_saved_unseen_supplement(
    native_reply_case, pending
):
    result, first, _ = prepare_revisit(native_reply_case, pending=pending)
    database, owner, runtime, _, tasks, task = result
    before = report(result, first)
    assert before["eligible"], before
    with database.snapshot() as connection:
        cursor = input_cursor(connection, owner, "work")
    if not pending:
        journal = PrivateJournal(runtime.journal.path.parent, initialize=False)
        reopened = WorkRuntime(database, journal, runtime.clock)
        launcher = WorkLauncher(reopened, result[3].config, journal.installation())
        result = (database, owner, reopened, launcher, tasks, task)
    received = tick(result)
    assert received.outcome == "completed" and received.task_id == task["id"]
    assert received.turns == 1 and received.window_id != first
    requests = starts(native_reply_case)
    assert len(requests) == 2 and len({row["params"]["threadId"] for row in requests}) == 1
    payload = json.loads(requests[-1]["params"]["input"][0]["text"].rsplit("\n", 1)[-1])
    assert payload["revisit"]["internal"] == int(not pending)
    inputs = tasks.inputs(owner, task["id"], 0, 50)["inputs"]
    assert len(inputs) == 2 and inputs[-1]["source"] == ("visitor" if pending else "revisit")
    assert payload["input"]["id"] == inputs[-1]["id"] != task["initial_input"]["id"]
    with database.snapshot() as connection:
        assert input_cursor(connection, owner, "work") == cursor
        assert connection.execute("SELECT state FROM work_revisits").fetchone()[0] == "completed"
        assert connection.execute("SELECT count(*) FROM work_revisit_claims").fetchone()[0] == 1
    assert tick(result).outcome == "queue_empty"
    assert len(starts(native_reply_case)) == 2
    after = report(result, first)
    assert after["eligible"] and after["business_digest"] == before["business_digest"]
    assert report(result, received.window_id)["eligible"]
    assert not runtime.journal.outstanding()


@pytest.mark.parametrize("block", ["schedule", "reception", "budget", "service"])
def test_due_work_keeps_one_intent_through_real_intake_gates(native_reply_case, block):
    result, _, services = prepare_revisit(native_reply_case)
    database, owner, runtime, _, tasks, task = result
    original = tasks.get(owner, task["id"])["decision"]["revisit"]
    schedule = native_reply_case[0][0][3]
    if block == "schedule":
        schedule.pause(True)
    elif block == "reception":
        with database.connect(write=True) as connection:
            save(
                connection,
                owner,
                {"scope": "work", "mode": "focusing", "resume_on": "explicit", "until": None},
                int(runtime.clock()),
            )
    elif block == "budget":
        ExecutionBudget(database, runtime.clock).configure(
            ExecutionPolicy(daily_launches=1, daily_seconds=None)
        )
    else:
        services.disable("work", owner)
    assert tick(result).outcome != "completed"
    assert len(starts(native_reply_case)) == 1
    blocked = tasks.get(owner, task["id"])["decision"]["revisit"]
    assert blocked["id"] == original["id"] and blocked["revision"] == original["revision"]
    assert blocked["state"] == "blocked" and blocked["blocked_reason"] == block
    if block == "schedule":
        schedule.pause(False)
    elif block == "reception":
        with database.connect(write=True) as connection:
            save(
                connection,
                owner,
                {"scope": "work", "mode": "available", "resume_on": None, "until": None},
                int(runtime.clock()),
            )
    elif block == "budget":
        ExecutionBudget(database, runtime.clock).configure(
            ExecutionPolicy(daily_launches=200, daily_seconds=None)
        )
    else:
        services.resume("work", owner)
    assert tick(result).outcome == "completed"
    assert len(starts(native_reply_case)) == 2


def test_suspended_stop_never_launches_a_return(native_reply_case):
    result, _, _ = prepare_revisit(native_reply_case)
    _, owner, _, _, tasks, task = result
    tasks.stop(owner, task["id"], str(uuid4()))
    assert tick(result).outcome == "queue_empty"
    assert len(starts(native_reply_case)) == 1
    assert tasks.get(owner, task["id"])["status"] == "cancelled"


def test_unknown_return_keeps_occupancy_then_replays_original_choice(
    native_reply_case, monkeypatch
):
    result, _, _ = prepare_revisit(native_reply_case)
    database, owner, runtime, _, tasks, task = result
    native_reply_case[4][0].stop_confirmed = False
    with pytest.raises(ProtocolError, match="requires reconciliation"):
        tick(result)
    saved = tasks.get(owner, task["id"])
    assert saved["status"] == "needs_reconciliation"
    assert saved["decision"]["revisit"]["state"] == "needs_reconciliation"
    with database.snapshot() as connection:
        window = connection.execute(
            "SELECT id FROM execution_windows WHERE state='unknown'"
        ).fetchone()[0]
    WorkRecovery(runtime).replay_results(owner, window)
    assert tick(result).outcome == "run_busy"
    monkeypatch.setattr("milo_home.work_reconciliation.stop_family", lambda _: True)
    assert WorkReconciliation(runtime).settle(owner, window)["closed"]
    assert tasks.get(owner, task["id"])["decision"]["state"] == "accepted"
    assert report(result, window)["eligible"]
    assert tick(result).outcome == "queue_empty" and len(starts(native_reply_case)) == 2

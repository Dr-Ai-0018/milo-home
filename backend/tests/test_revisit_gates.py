"""Due revisits retain one intention across blockers, restart and simultaneous selection."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from test_native_reply_execution import native_reply_case as native_reply_case
from test_native_reply_execution import requests, tick
from test_reception import request
from test_reply_readiness import preparation as preparation
from test_revisit_native import deferred
from test_work_process import fake_units as fake_units

from milo_home.database import Database
from milo_home.dispatch import DispatchStore
from milo_home.execution_budget import ExecutionBudget, ExecutionPolicy
from milo_home.reception import save
from milo_home.reply_decisions import revisit_summary
from milo_home.reply_scope import ReplyScope
from milo_home.revisit_control import RevisitControl
from milo_home.runs import RunStore


@pytest.mark.parametrize("gate", ["schedule", "reception", "budget", "service"])
def test_due_blockers_preserve_arrangement_and_resume_only_once(client, native_reply_case, gate):
    case, database, owner, _, scenario, intent, _ = deferred(client, native_reply_case)
    case[1][0] = intent["due_at"]
    schedule = case[0][3]
    schedule.clock = lambda: case[1][0]
    budget = ExecutionBudget(database, lambda: case[1][0])
    if gate == "schedule":
        schedule.pause(True)
    elif gate == "reception":
        with database.connect(write=True) as connection:
            save(connection, owner, request("reply"), case[1][0])
    elif gate == "budget":
        budget.configure(ExecutionPolicy(daily_launches=1, daily_seconds=None))
    else:
        case[2].disable("reply", owner)
    before = len(requests(native_reply_case))
    assert tick(native_reply_case)["outcome"] != "succeeded"
    assert len(requests(native_reply_case)) == before
    with database.snapshot() as connection:
        summary = revisit_summary(connection, intent["conversation_id"], case[1][0])
        assert summary.pending_count == 1 and summary.blocked_count == 1
        assert (
            connection.execute(
                "SELECT revision FROM revisit_intents WHERE id=?", (intent["id"],)
            ).fetchone()[0]
            == 1
        )
    if gate == "schedule":
        schedule.pause(False)
    elif gate == "reception":
        with database.connect(write=True) as connection:
            save(connection, owner, request("reply", "available", None), case[1][0])
    elif gate == "budget":
        budget.configure(ExecutionPolicy(daily_launches=200, daily_seconds=None))
    else:
        case[2].resume("reply", owner)
    scenario[0] = "auto-reply-followup"
    assert tick(native_reply_case)["outcome"] == "succeeded"
    before = len(requests(native_reply_case))
    assert tick(native_reply_case)["outcome"] == "waiting_or_other_entry_first"
    assert len(requests(native_reply_case)) == before


def test_restart_finds_saved_revisit_and_concurrent_selection_has_one_winner(
    client, native_reply_case
):
    case, database, owner, _, _, intent, _ = deferred(client, native_reply_case)
    case[1][0] = intent["due_at"]
    case[2].observe("reply", owner, timer_active=True)
    reopened = Database(database.path)

    def reserve():
        return DispatchStore(
            reopened, lambda: case[1][0], scope=ReplyScope(owner), include_revisits=True
        ).reserve()

    with ThreadPoolExecutor(max_workers=3) as pool:
        tickets = list(pool.map(lambda _: reserve(), range(3)))
    winner = next(item for item in tickets if item is not None)
    assert sum(item is not None for item in tickets) == 1
    with reopened.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM revisit_claims").fetchone()[0] == 1
    changed = RevisitControl(reopened, lambda: case[1][0]).change(
        owner, intent["id"], 1, action="reschedule", due_at=case[1][0] + 120
    )
    dispatch = DispatchStore(
        reopened, lambda: case[1][0], scope=ReplyScope(owner), include_revisits=True
    )
    assert not dispatch.authorize_launch(winner)
    with reopened.snapshot() as connection:
        row = connection.execute(
            "SELECT * FROM revisit_intents WHERE id=?", (intent["id"],)
        ).fetchone()
        assert row["revision"] == changed["revision"] == 2 and row["due_at"] == changed["due_at"]
    # No process was ever created for this reservation; retire that known unlaunched run.
    RunStore(reopened, lambda: case[1][0]).finish_failed(winner.run_id, "cancelled")
    dispatch.reconcile()
    case[1][0] = changed["due_at"]
    native_reply_case[3][0] = "auto-reply-followup"
    assert tick(native_reply_case)["outcome"] == "succeeded"


def test_reception_closes_before_first_native_send_and_safe_retry_has_a_new_revision(
    client, native_reply_case, monkeypatch
):
    from milo_home.native_reply_delivery import NativeReplyDelivery

    case, database, owner, _, scenario, intent, _ = deferred(client, native_reply_case)
    case[1][0] = intent["due_at"]
    scenario[0] = "auto-reply-followup"
    authorize = NativeReplyDelivery._authorize
    paused = []

    def pause_once(self, input_id):
        if not paused:
            with database.connect(write=True) as connection:
                save(connection, owner, request("reply"), case[1][0])
            paused.append(True)
        return authorize(self, input_id)

    monkeypatch.setattr(NativeReplyDelivery, "_authorize", pause_once)
    before = sum(row["method"] == "turn/start" for row in requests(native_reply_case))
    assert tick(native_reply_case)["outcome"] == "cancelled"
    with database.snapshot() as connection:
        saved = dict(
            connection.execute(
                "SELECT * FROM revisit_intents WHERE id=?", (intent["id"],)
            ).fetchone()
        )
        assert saved["state"] == "scheduled" and saved["revision"] == 2
        assert saved["initial_due_at"] == intent["initial_due_at"]
        assert (
            connection.execute(
                "SELECT fault_code FROM automatic_services WHERE kind='reply'"
            ).fetchone()[0]
            is None
        )
    assert sum(row["method"] == "turn/start" for row in requests(native_reply_case)) == before
    with database.connect(write=True) as connection:
        save(connection, owner, request("reply", "available", None), case[1][0])
    case[1][0] = saved["due_at"]
    assert tick(native_reply_case)["outcome"] == "succeeded"

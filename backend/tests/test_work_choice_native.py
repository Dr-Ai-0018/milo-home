"""Real work consumer and isolated native protocol; no paid model or production changes."""

import json
import sqlite3
from uuid import uuid4

import pytest
from test_native_reply_execution import native_reply_case as native_reply_case
from test_reply_readiness import preparation as preparation
from test_work_process import fake_units as fake_units

from milo_home.codex_config import NewawzConfig
from milo_home.codex_rpc import ProtocolError
from milo_home.reception import current, save
from milo_home.work_decision_native import WORK_CHOICE_SCHEMA
from milo_home.work_process import WorkLauncher
from milo_home.work_reconciliation import WorkReconciliation
from milo_home.work_recovery import WorkRecovery
from milo_home.work_runtime import WorkRuntime
from milo_home.work_task_receipt import read_receipt
from milo_home.work_tasks import WorkTasks


def setup_work(native_reply_case, scenario):
    case, journal, _, selected, _ = native_reply_case
    database, owner, _, _, _, profile, _ = case[0]
    runtime = WorkRuntime(database, journal, lambda: case[1][0])
    launcher = WorkLauncher(runtime, NewawzConfig.load(profile), journal.installation())
    tasks = WorkTasks(database, runtime.clock)
    session = journal.business_binding().session_id
    task, _ = tasks.create(owner, session, "合成交办，允许先问清楚", str(uuid4()))
    selected[0] = scenario
    return database, owner, runtime, launcher, tasks, task


def starts(native_reply_case):
    return [
        json.loads(line)
        for line in native_reply_case[2].read_text().splitlines()
        if json.loads(line)["method"] == "turn/start"
    ]


@pytest.mark.parametrize(
    "scenario,decision",
    [
        ("auto-work-deferred", "deferred"),
        ("auto-work-declined", "declined"),
        ("auto-work-clarify", "needs_clarification"),
    ],
)
def test_native_choice_publishes_receipt_and_stops_without_processing_remaining_inputs(
    native_reply_case, scenario, decision
):
    database, owner, runtime, launcher, tasks, task = setup_work(native_reply_case, scenario)
    tasks.append(owner, task["id"], "合成开始前的补充", str(uuid4()))
    window, result = launcher.run_task(
        owner, task["id"], 60, credential="synthetic-private-file-key"
    )
    saved = tasks.get(owner, task["id"])
    assert result.outcome == "completed" and result.turns == 1
    assert saved["status"] == "completed" and saved["decision"]["state"] == decision
    assert read_receipt(database, owner, task["id"]) == {
        "state": "available",
        "text": "合成工作回执",
    }
    requests = starts(native_reply_case)
    assert len(requests) == 1 and requests[0]["params"]["outputSchema"] == WORK_CHOICE_SCHEMA
    assert requests[0]["params"]["threadId"] == runtime.journal.binding().thread_id
    assert not runtime.journal.outstanding()
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM work_turn_bases").fetchone()[0] == 1
        assert (
            connection.execute("SELECT output FROM work_results").fetchone()[0].find('"version"')
            == -1
        )
    assert WorkRecovery(runtime).replay_results(owner, window) == 1
    assert len(starts(native_reply_case)) == 1


def test_clarification_second_window_delivers_only_new_input_on_same_thread(native_reply_case):
    database, owner, runtime, launcher, tasks, task = setup_work(
        native_reply_case, "auto-work-clarify"
    )
    first, _ = launcher.run_task(owner, task["id"], 60, credential="synthetic-private-file-key")
    native_reply_case[0][1][0] += 1
    extra, _ = tasks.append(owner, task["id"], "合成明确答复：按蓝色做", str(uuid4()))
    native_reply_case[3][0] = "auto-work-accepted"
    second, result = launcher.run_task(
        owner, task["id"], 60, credential="synthetic-private-file-key"
    )
    assert result.outcome == "completed" and result.turns == 1 and first != second
    requests = starts(native_reply_case)
    assert len(requests) == 2
    assert requests[1]["params"]["clientUserMessageId"] == extra["id"]
    assert len({row["params"]["threadId"] for row in requests}) == 1
    assert tasks.get(owner, task["id"])["decision"]["state"] == "accepted"
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM work_tasks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM work_decisions").fetchone()[0] == 2


def test_sealed_choice_waits_for_confirmed_stop_then_recovers_without_another_turn(
    native_reply_case, monkeypatch
):
    database, owner, runtime, launcher, tasks, task = setup_work(
        native_reply_case, "auto-work-clarify"
    )
    native_reply_case[4][0].stop_confirmed = False
    with pytest.raises(ProtocolError, match="requires reconciliation"):
        launcher.run_task(owner, task["id"], 60, credential="synthetic-private-file-key")
    assert tasks.get(owner, task["id"])["status"] == "needs_reconciliation"
    with database.snapshot() as connection:
        window = connection.execute("SELECT id FROM execution_windows").fetchone()[0]
    assert WorkRecovery(runtime).replay_results(owner, window) == 1
    assert tasks.get(owner, task["id"])["decision"] is None
    monkeypatch.setattr("milo_home.work_reconciliation.stop_family", lambda _: True)
    assert WorkReconciliation(runtime).settle(owner, window)["closed"]
    assert tasks.get(owner, task["id"])["decision"]["state"] == "needs_clarification"
    assert len(starts(native_reply_case)) == 1


def test_late_work_reception_cannot_override_a_later_independent_arrangement(native_reply_case):
    database, owner, runtime, launcher, tasks, task = setup_work(
        native_reply_case, "auto-work-reception"
    )
    with database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER publication_outage BEFORE INSERT ON work_decisions "
            "BEGIN SELECT RAISE(ABORT,'synthetic publication outage'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic publication outage"):
        launcher.run_task(owner, task["id"], 60, credential="synthetic-private-file-key")
    with database.connect(write=True) as connection:
        window = connection.execute("SELECT id FROM execution_windows").fetchone()[0]
        connection.execute("DROP TRIGGER publication_outage")
        revision = save(
            connection,
            owner,
            {"scope": "work", "mode": "focusing", "resume_on": "explicit", "until": None},
            int(runtime.clock()),
        )
    assert WorkRecovery(runtime).replay_results(owner, window) == 1
    assert WorkRecovery(runtime).replay_results(owner, window) == 1
    with database.snapshot() as connection:
        assert current(connection, owner, "work")["revision"] == revision
        assert current(connection, owner, "reply") is None
        assert (
            connection.execute(
                "SELECT c.applied FROM reception_changes c JOIN work_result_receptions p "
                "ON p.owner_id=c.owner_id AND p.revision=c.revision"
            ).fetchone()[0]
            == 0
        )
    assert len(starts(native_reply_case)) == 1

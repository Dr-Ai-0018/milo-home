"""Decisions have real result origins, survive reopening, and resume the original task."""

import sqlite3
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case

from milo_home.database import Database
from milo_home.errors import ApiError
from milo_home.work_decision_publication import publish_decision
from milo_home.work_task_receipt import read_receipt
from milo_home.work_tasks import WorkTasks


def decided(launch_case, decision):
    case, _, _, launcher, _, _ = launch_case
    task, window = reserve(case)
    owner, database = case[0][1], case[0][0]
    launcher.run(owner, window["id"], credential="synthetic-secret")
    payload = {
        "version": 1,
        "decision": decision,
        "receipt": "合成接单决定",
        "revisit_at": case[-1][0] + 60 if decision == "deferred" else None,
        "reception": None,
    }
    with database.connect(write=True) as connection:
        result = dict(connection.execute("SELECT * FROM work_results").fetchone())
        assert publish_decision(connection, result, payload, case[-1][0])
        assert not publish_decision(connection, result, payload, case[-1][0] + 1)
    return case, task, result, payload


@pytest.mark.parametrize("decision", ["deferred", "declined", "needs_clarification"])
def test_decision_receipt_groups_and_reopen_keep_real_state(
    launch_case, fake_units, decision, tmp_path
):
    case, task, result, payload = decided(launch_case, decision)
    database, owner, _, _, _, _, session = case[0]
    tasks = WorkTasks(database, lambda: case[-1][0])
    saved = tasks.get(owner, task["id"])
    assert saved["status"] == "completed"
    assert saved["decision"]["state"] == decision
    assert saved["decision"]["revision"] == 1
    assert read_receipt(database, owner, task["id"]) == {
        "state": "available",
        "text": payload["receipt"],
    }
    group = "finished" if decision == "declined" else "active"
    assert [r["id"] for r in tasks.history(owner, session["id"], None, 10, group)["tasks"]] == [
        task["id"]
    ]
    opposite = "active" if group == "finished" else "finished"
    assert tasks.history(owner, session["id"], None, 10, opposite)["tasks"] == []
    backup = tmp_path / "restored.sqlite3"
    database.backup(backup)
    assert WorkTasks(Database(backup)).get(owner, task["id"])["decision"] == saved["decision"]
    with database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE work_decisions SET receipt='rewrite'")
        with pytest.raises(ValueError, match="differs"):
            publish_decision(
                connection, result, payload | {"receipt": "改写旧结果"}, case[-1][0] + 1
            )


def test_clarification_resumes_same_task_once_and_stop_prevents_resurrection(
    launch_case, fake_units
):
    case, task, _, _ = decided(launch_case, "needs_clarification")
    database, owner, other, _, _, _, _ = case[0]
    tasks = WorkTasks(database, lambda: case[-1][0] + 1)
    key = str(uuid4())
    extra, saved = tasks.append(owner, task["id"], "这是合成的补充说明", key)
    assert saved and extra["source"] == "visitor"
    assert tasks.append(owner, task["id"], "这是合成的补充说明", key) == (extra, False)
    assert tasks.get(owner, task["id"])["status"] == "queued"
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM work_tasks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM work_continuations").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM execution_windows").fetchone()[0] == 1
    with pytest.raises(ApiError, match="work_not_found"):
        tasks.append(other, task["id"], "不能跨账号", str(uuid4()))
    assert tasks.stop(owner, task["id"], str(uuid4()))["status"] == "cancelled"
    with pytest.raises(ApiError, match="work_input_conflict"):
        tasks.append(owner, task["id"], "停止后不能复活", str(uuid4()))


def test_deferred_stop_cancels_arrangement_without_inventing_a_decline(launch_case, fake_units):
    case, task, _, _ = decided(launch_case, "deferred")
    database, owner, _, _, _, _, session = case[0]
    tasks = WorkTasks(database, lambda: case[-1][0] + 1)
    stopped = tasks.stop(owner, task["id"], str(uuid4()))
    assert stopped["status"] == "cancelled" and stopped["decision"] is None
    assert tasks.history(owner, session["id"], None, 10, "active")["tasks"] == []
    with database.snapshot() as connection:
        assert connection.execute("SELECT state FROM work_revisits").fetchone()[0] == "cancelled"
        assert connection.execute("SELECT state FROM work_decisions").fetchone()[0] == "deferred"

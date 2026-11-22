import json
import os
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest
from test_automatic_worker import successful_execute
from test_dispatch import make_case
from test_runs import result
from test_worker import fake_runtime

from milo_home import worker as module
from milo_home.database import Database
from milo_home.dispatch import DispatchStore
from milo_home.errors import ApiError
from milo_home.schedules import ScheduleStore
from milo_home.wakeup_events import EventPlanner
from milo_home.worker import Worker


def test_prelaunch_recovery_retries_at_most_three_reservations(client, monkeypatch):
    _, runs, dispatch, now = make_case(client)
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)
    worker = Worker(runs.database, clock=runs.clock)
    old_tickets = []
    for attempt in range(1, 4):
        ticket = dispatch.reserve(120)
        assert ticket is not None
        old_tickets.append(ticket)
        now[0] = ticket.lease_until
        assert not dispatch.authorize_launch(ticket)
        assert worker.recover() == ticket.run_id
        assert not dispatch.authorize_launch(ticket)
        assert not runs.publish(ticket.run_id, result(runs, ticket.run_id))
        assert dispatch.reconcile()["settled_runs"] == 0
        assert dispatch.reserve() is None
        if attempt < 3:
            retry_at = now[0] + 30 * attempt
            now[0] = retry_at - 1
            assert dispatch.reserve() is None
            now[0] = retry_at
    now[0] += 3600
    assert dispatch.reserve() is None
    assert len({ticket.run_id for ticket in old_tickets}) == 3
    with runs.database.connect() as connection:
        deliveries = connection.execute("SELECT * FROM wake_event_delivery").fetchall()
        assert all(
            row["attempts"] == 3 and row["blocked_reason"] == "retry_exhausted"
            for row in deliveries
        )
        assert connection.execute("SELECT count(*) FROM wake_run_events").fetchone()[0] == 6


@pytest.mark.parametrize("failure", ["configuration_error", "execution_failed", "invalid_result"])
def test_deterministic_or_possibly_billed_failure_is_not_retried(client, failure):
    _, runs, dispatch, now = make_case(client)
    ticket = dispatch.reserve(60)
    if failure != "configuration_error":
        assert dispatch.authorize_launch(ticket)
        runs.started(ticket.run_id)
    runs.finish_failed(ticket.run_id, failure)
    summary = dispatch.reconcile()
    assert summary["blocked_messages"] == 2
    assert dispatch.reconcile()["settled_runs"] == 0
    now[0] += 600
    assert dispatch.reserve() is None


def test_new_message_can_run_without_retrying_blocked_old_messages(client):
    thread, runs, dispatch, now = make_case(client)
    ticket = dispatch.reserve(60)
    assert dispatch.authorize_launch(ticket)
    runs.started(ticket.run_id)
    runs.finish_failed(ticket.run_id, "execution_failed")
    dispatch.reconcile()
    old_ids = {message["id"] for message in runs.snapshot(ticket.run_id)["selected"]}
    response = client.post(
        f"/api/threads/{thread}/messages",
        headers={"Idempotency-Key": str(uuid4())},
        json={"body": "失败之后的新合成留言"},
    )
    EventPlanner(runs.database, runs.clock).plan()
    now[0] += 15
    next_ticket = dispatch.reserve(60)
    assert next_ticket is not None
    selected = runs.snapshot(next_ticket.run_id)["selected"]
    assert [message["id"] for message in selected] == [response.json()["id"]]
    assert not old_ids.intersection(message["id"] for message in selected)


@pytest.mark.parametrize("manual_stop_first", [False, True])
def test_pause_keeps_its_source_distinct_from_a_manual_stop(client, manual_stop_first):
    _, runs, dispatch, now = make_case(client)
    ticket = dispatch.reserve(60)
    if manual_stop_first:
        runs.stop(ticket.run_id)
    schedule = ScheduleStore(runs.database, runs.clock)
    schedule.pause(True)
    runs.checkpoint(ticket.run_id)
    runs.finish_failed(ticket.run_id, "interrupted")
    summary = dispatch.reconcile()
    assert summary["blocked_messages" if manual_stop_first else "retrying_messages"] == 2
    schedule.pause(False)
    now[0] += 30
    assert (dispatch.reserve(60) is None) == manual_stop_first


def test_unconfirmed_process_cannot_be_settled_by_management_cli(client, monkeypatch):
    _, runs, dispatch, _ = make_case(client)
    ticket = dispatch.reserve(60)
    assert dispatch.authorize_launch(ticket)
    runs.started(ticket.run_id)
    runs.stop(ticket.run_id, "interrupted")
    monkeypatch.setattr(module, "stop_unit", lambda unit: False)
    with pytest.raises(ApiError, match="termination_unknown"):
        Worker(runs.database, clock=runs.clock).recover()
    process = subprocess.run(
        [sys.executable, "-m", "milo_home.cli", "events", "reconcile"],
        env={**os.environ, "MILO_DATABASE": str(runs.database.path)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["settled_runs"] == 0
    assert ticket.token not in process.stdout + process.stderr
    assert runs.active()["id"] == ticket.run_id
    assert dispatch.reserve() is None
    status = subprocess.run(
        [sys.executable, "-m", "milo_home.cli", "events", "status"],
        env={**os.environ, "MILO_DATABASE": str(runs.database.path)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["deliveries"][0]["state"] == "reserved"
    assert ticket.token not in status.stdout + status.stderr


def test_success_is_settled_and_never_dispatched_again(client, monkeypatch):
    _, runs, dispatch, now = make_case(client)
    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "execute", successful_execute)
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)
    worker = Worker(runs.database, clock=runs.clock)
    assert worker.wake_automatic(60)["status"] == "succeeded"
    now[0] += 60
    assert worker.wake_automatic(60) is None
    assert dispatch.reconcile()["settled_runs"] == 0
    assert all(
        row["state"] == "done" for row in EventPlanner(runs.database, runs.clock).status()["counts"]
    )


def test_automatic_iteration_recovers_orphan_but_leaves_manual_occupancy(client, monkeypatch):
    thread, runs, dispatch, now = make_case(client)
    ticket = dispatch.reserve(120)
    now[0] = ticket.lease_until
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)
    worker = Worker(runs.database, clock=runs.clock)
    assert worker.wake_automatic(60) is None
    assert runs.get(ticket.run_id)["status"] == "failed"
    manual = runs.queue(thread, 60)
    assert worker.wake_automatic(60) is None
    assert runs.active()["id"] == manual


def test_reconciliation_rollback_does_not_release_only_half_the_batch(client):
    _, runs, dispatch, _ = make_case(client)
    ticket = dispatch.reserve(60)
    runs.finish_failed(ticket.run_id, "configuration_error")
    with runs.database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_settle BEFORE UPDATE OF settled_at ON wake_dispatches "
            "BEGIN SELECT RAISE(ABORT, 'synthetic settle failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic settle failure"):
        dispatch.reconcile()
    with runs.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM wake_event_delivery WHERE run_id IS NOT NULL"
            ).fetchone()[0]
            == 2
        )
        assert connection.execute("SELECT settled_at FROM wake_dispatches").fetchone()[0] is None


def test_backup_keeps_blocked_delivery_and_attempt_history(client, tmp_path):
    _, runs, dispatch, _ = make_case(client)
    ticket = dispatch.reserve(60)
    assert dispatch.authorize_launch(ticket)
    runs.started(ticket.run_id)
    runs.finish_failed(ticket.run_id, "execution_failed")
    dispatch.reconcile()
    destination = tmp_path / "blocked-backup.sqlite3"
    runs.database.backup(destination)
    restored = Database(destination)
    restored.initialize()
    assert DispatchStore(restored, runs.clock).reserve(60) is None
    with restored.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM wake_run_events").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT count(*) FROM wake_event_delivery WHERE blocked_reason = 'execution_failed'"
            ).fetchone()[0]
            == 2
        )

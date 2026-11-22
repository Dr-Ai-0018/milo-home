from dataclasses import replace

import pytest
from test_dispatch import make_case
from test_runs import result
from test_schedules import timestamp
from test_worker import fake_runtime

from milo_home import worker as module
from milo_home.errors import ApiError
from milo_home.run_transport import EventStream
from milo_home.schedules import ScheduleStore
from milo_home.worker import Worker


def successful_execute(command, prompt, store, run_id):
    store.started(run_id)
    events = EventStream()
    events.completed = 1
    return 0, events, True


def test_launch_token_is_one_use_and_lease_only_covers_start(client):
    _, runs, dispatch, now = make_case(client)
    ticket = dispatch.reserve(60)
    assert not dispatch.authorize_launch(replace(ticket, token="incorrect-synthetic-token"))
    assert runs.get(ticket.run_id)["status"] == "queued"
    assert dispatch.authorize_launch(ticket)
    assert not dispatch.authorize_launch(ticket)
    runs.started(ticket.run_id)
    now[0] += 31
    assert runs.publish(ticket.run_id, result(runs, ticket.run_id))


def test_expired_start_lease_keeps_occupancy_until_confirmed(client):
    _, runs, dispatch, now = make_case(client)
    ticket = dispatch.reserve(60)
    now[0] = ticket.lease_until
    assert not dispatch.authorize_launch(ticket)
    assert runs.get(ticket.run_id)["status"] == "stopping"
    assert runs.active()["id"] == ticket.run_id
    assert dispatch.reserve(60) is None


def test_missing_launch_mark_cannot_be_started_or_published(client):
    _, runs, dispatch, _ = make_case(client)
    ticket = dispatch.reserve(60)
    runs.started(ticket.run_id)
    assert runs.get(ticket.run_id)["status"] == "stopping"
    assert not runs.publish(ticket.run_id, result(runs, ticket.run_id))
    with runs.database.connect() as connection:
        assert (
            connection.execute("SELECT abort_reason FROM wake_dispatches").fetchone()[0]
            == "launch_missing"
        )


@pytest.mark.parametrize(
    "phase", ["resting", "winding_down", "paused", "unconfigured", "clock_rewind"]
)
def test_checkpoint_and_publication_recheck_automatic_work(client, phase):
    _, runs, dispatch, now = make_case(client)
    ticket = dispatch.reserve(60)
    assert dispatch.authorize_launch(ticket)
    runs.started(ticket.run_id)
    schedule = ScheduleStore(runs.database, runs.clock)
    if phase == "clock_rewind":
        now[0] -= 1
    elif phase == "paused":
        schedule.pause(True)
    elif phase == "unconfigured":
        with runs.database.connect(write=True) as connection:
            connection.execute("UPDATE schedule_state SET policy = NULL WHERE id = 1")
    else:
        schedule.override(phase, now[0] + 60)
    assert not runs.publish(ticket.run_id, result(runs, ticket.run_id))
    assert runs.checkpoint(ticket.run_id)["status"] == "stopping"
    assert client.get("/api/threads").json()[0]["pending_count"] == 2
    runs.finish_failed(ticket.run_id, "invalid_result")
    assert runs.get(ticket.run_id)["failure_code"] == (
        "interrupted" if phase == "clock_rewind" else "cancelled"
    )


def test_exact_work_window_end_stops_before_deadline(client):
    _, runs, dispatch, now = make_case(client)
    now[0] = timestamp("11:59") + 45
    ticket = dispatch.reserve(60)
    assert dispatch.authorize_launch(ticket)
    runs.started(ticket.run_id)
    now[0] = timestamp("12:00")
    run = runs.checkpoint(ticket.run_id)
    assert run["stop_reason"] == "cancelled"
    assert not runs.publish(ticket.run_id, result(runs, ticket.run_id))


def test_internal_worker_uses_real_publication_without_model_calls(client, monkeypatch):
    thread, runs, _, _ = make_case(client)
    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "execute", successful_execute)
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)
    run = Worker(runs.database, clock=runs.clock).wake_automatic(60)
    assert run["status"] == "succeeded"
    assert client.get("/api/threads").json()[0]["pending_count"] == 0
    messages = client.get(f"/api/threads/{thread}/messages").json()["messages"]
    assert [message["role"] for message in messages] == ["visitor", "visitor", "milo", "milo"]
    assert not (runs.database.path.parent / "runs" / run["id"] / "credential.env").exists()


@pytest.mark.parametrize("change", ["resting", "lease_expired"])
def test_prepare_changes_prevent_process_launch(client, monkeypatch, change):
    _, runs, _, now = make_case(client)

    def prepare(base, snapshot, profile):
        root = fake_runtime(base, snapshot, profile)
        if change == "resting":
            ScheduleStore(runs.database, runs.clock).override("resting", now[0] + 60)
        else:
            now[0] += 30
        return root

    def forbidden(*args):
        raise AssertionError("Must not launch a process after the start guard fails")

    monkeypatch.setattr(module, "prepare", prepare)
    monkeypatch.setattr(module, "execute", forbidden)
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)
    run = Worker(runs.database, clock=runs.clock).wake_automatic(60)
    assert run["status"] == ("cancelled" if change == "resting" else "failed")
    with runs.database.connect() as connection:
        assert connection.execute("SELECT launched_at FROM wake_dispatches").fetchone()[0] is None


def test_window_closes_after_completion_but_before_publication(client, monkeypatch):
    _, runs, _, now = make_case(client)

    def execute(command, prompt, store, run_id):
        success = successful_execute(command, prompt, store, run_id)
        ScheduleStore(runs.database, runs.clock).override("resting", now[0] + 60)
        return success

    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "execute", execute)
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)
    run = Worker(runs.database, clock=runs.clock).wake_automatic(60)
    assert run["status"] == "cancelled"
    assert client.get("/api/threads").json()[0]["pending_count"] == 2


def test_unknown_automatic_process_is_never_replaced(client, monkeypatch):
    _, runs, dispatch, now = make_case(client)
    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "execute", lambda *args: (0, EventStream(), False))
    worker = Worker(runs.database, clock=runs.clock)
    with pytest.raises(ApiError, match="termination_unknown"):
        worker.wake_automatic(60)
    active = runs.active()
    now[0] += 120
    assert dispatch.reserve(60) is None
    monkeypatch.setattr(module, "stop_unit", lambda unit: False)
    with pytest.raises(ApiError, match="termination_unknown"):
        worker.recover()
    assert runs.active()["id"] == active["id"]
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)
    assert worker.recover() == active["id"]
    assert runs.active() is None

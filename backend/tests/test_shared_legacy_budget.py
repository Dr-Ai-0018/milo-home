from uuid import uuid4

import pytest
from test_dispatch import make_case
from test_worker import fake_runtime

from milo_home import worker as module
from milo_home.automatic_budget import AutomaticPolicy
from milo_home.dispatch import DispatchStore
from milo_home.errors import ApiError
from milo_home.execution_budget import ExecutionBudget, ExecutionPolicy
from milo_home.run_transport import EventStream
from milo_home.work_access import WorkAccess
from milo_home.work_tasks import WorkTasks
from milo_home.worker import Worker


def configure(runs, launches=1, seconds=60):
    budget = ExecutionBudget(runs.database, runs.clock)
    budget.configure(ExecutionPolicy(daily_launches=launches, daily_seconds=seconds))
    return budget


def success(command, prompt, store, run_id):
    assert store.get(run_id)["status"] == "queued"
    with store.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT authorized_at FROM legacy_launches WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            is not None
        )
    store.started(run_id)
    events = EventStream()
    events.completed = 1
    return 0, events, True


@pytest.mark.parametrize("automatic", [False, True])
def test_manual_and_automatic_worker_consume_common_allowance(client, monkeypatch, automatic):
    thread, runs, _, now = make_case(client)
    budget = configure(runs)
    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "execute", success)
    monkeypatch.setattr(module, "stop_unit", lambda _: True)
    worker = Worker(runs.database, clock=runs.clock)
    run = worker.wake_automatic(60) if automatic else worker.wake(thread, 60)
    assert run["status"] == "succeeded"
    report = budget.view()
    assert report["held_or_charged_launches"] == 1
    assert report["held_or_charged_seconds"] == 1
    now[0] += 15
    client.post(
        f"/api/threads/{thread}/messages",
        headers={"Idempotency-Key": str(uuid4())},
        json={"body": "新的合成来信"},
    )
    from milo_home.wakeup_events import EventPlanner

    EventPlanner(runs.database, runs.clock).plan()
    now[0] += 15
    with pytest.raises(ApiError, match="daily_budget_exhausted"):
        DispatchStore(runs.database, runs.clock).reserve(60)
    with pytest.raises(ApiError, match="daily_budget_exhausted"):
        runs.queue(thread, 60)
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_local_automatic_policy_cannot_override_shared_policy(client):
    _, runs, _, _ = make_case(client)
    configure(runs, seconds=3)
    local = AutomaticPolicy(daily_launches=None, daily_seconds=None)
    dispatch = DispatchStore(runs.database, runs.clock, policy=local)
    ticket = dispatch.reserve(60)
    assert runs.get(ticket.run_id)["deadline_at"] - int(runs.clock()) == 3
    assert dispatch.authorize_launch(ticket)
    assert runs.authorize_launch(ticket.run_id)
    assert not runs.authorize_launch(ticket.run_id)


def test_policy_reduction_during_prepare_prevents_actual_execute(client, monkeypatch):
    thread, runs, _, _ = make_case(client)
    budget = configure(runs, seconds=60)

    def prepare(base, snapshot, profile):
        root = fake_runtime(base, snapshot, profile)
        budget.configure(ExecutionPolicy(daily_launches=1, daily_seconds=1))
        return root

    def forbidden(*args):
        raise AssertionError("No process may start after the shared budget closes")

    monkeypatch.setattr(module, "prepare", prepare)
    monkeypatch.setattr(module, "execute", forbidden)
    monkeypatch.setattr(module, "stop_unit", lambda _: True)
    assert Worker(runs.database, clock=runs.clock).wake(thread, 60)["status"] == "cancelled"
    assert budget.view()["held_or_charged_launches"] == 0


def test_missing_pre_spawn_marker_blocks_start_and_midrun_reduction_stops(client):
    thread, runs, _, _ = make_case(client)
    budget = configure(runs)
    first = runs.queue(thread, 60)
    runs.started(first)
    assert runs.get(first)["status"] == "stopping"
    runs.finish_failed(first, "configuration_error")
    second = runs.queue(thread, 60)
    assert runs.authorize_launch(second)
    runs.started(second)
    assert runs.get(second)["status"] == "running"
    budget.configure(ExecutionPolicy(daily_launches=1, daily_seconds=1))
    assert runs.checkpoint(second)["status"] == "stopping"


def test_new_work_usage_blocks_ready_automatic_dispatch_without_spending_attempt(client, tmp_path):
    from milo_home.execution_control import ExecutionControl
    from milo_home.execution_permits import ExecutionPermits

    thread, runs, dispatch, now = make_case(client)
    budget = configure(runs)
    with runs.database.connect() as connection:
        owner = connection.execute("SELECT owner_id FROM threads WHERE id=?", (thread,)).fetchone()[
            0
        ]
    access = WorkAccess(runs.database, runs.clock)
    workspace = str(uuid4())
    access.register_workspace(workspace, "合成工作区", tmp_path)
    access.grant(owner)
    tasks = WorkTasks(runs.database, runs.clock)
    session, _ = tasks.main_session(owner, workspace)
    task, _ = tasks.create(owner, session["id"], "合成任务", str(uuid4()))
    control = ExecutionControl(runs.database, runs.clock)
    permits = ExecutionPermits(runs.database, runs.clock)
    window = control.reserve(owner, task["id"], 60)
    permit = permits.reserve(owner, window["id"], task["initial_input"]["id"])
    assert permits.consume(owner, permit["id"], permit["input_id"])
    assert permits.finish(permit["id"], delivery_known=True)
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="completed")
    now[0] += 1
    assert budget.view()["held_or_charged_launches"] == 1
    with pytest.raises(ApiError, match="daily_budget_exhausted"):
        dispatch.reserve(60)
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM wake_event_delivery").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_legacy_launch_marker_cannot_bypass_automatic_ticket(client):
    _, runs, dispatch, _ = make_case(client)
    configure(runs)
    ticket = dispatch.reserve(60)
    assert not runs.authorize_launch(ticket.run_id)
    assert runs.get(ticket.run_id)["status"] == "stopping"
    with runs.database.connect() as connection:
        assert connection.execute("SELECT authorized_at FROM legacy_launches").fetchone()[0] is None

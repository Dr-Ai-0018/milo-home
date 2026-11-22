import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from test_dispatch import make_case
from test_runs import result

from milo_home.automatic_budget import AutomaticPolicy, budget_view, load_policy
from milo_home.dispatch import DispatchStore
from milo_home.errors import ApiError
from milo_home.schedule_policy import BEIJING, day_start
from milo_home.schedules import ScheduleStore


def view(runs, policy, timeout=60):
    with runs.database.connect() as connection:
        connection.execute("BEGIN")
        return budget_view(connection, policy, int(runs.clock()), timeout)


def incoming(client, thread, now):
    response = client.post(
        f"/api/threads/{thread}/messages",
        headers={"Idempotency-Key": str(uuid4())},
        json={"body": "预算测试中的另一封合成来信"},
    )
    assert response.status_code == 201
    now[0] += 15


@pytest.mark.parametrize(
    "content",
    [
        {},
        {"daily_launches": 1},
        {
            "daily_launches": True,
            "daily_seconds": 60,
        },
        {
            "daily_launches": 1,
            "daily_seconds": 0,
        },
        {
            "daily_launches": 1,
            "daily_seconds": None,
            "extra": 1,
        },
    ],
)
def test_policy_requires_explicit_strict_fields(content):
    with pytest.raises(ValueError):
        AutomaticPolicy.model_validate(content)


def test_private_policy_loads_explicit_unlimited_values_and_rejects_links(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "daily_launches": None,
                "daily_seconds": None,
            }
        )
    )
    path.chmod(0o600)
    assert load_policy(path).daily_launches is None
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        load_policy(link)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_policy(path)


def test_atomic_hold_launch_and_successful_settlement_survive_new_store(client):
    _, runs, _, now = make_case(client)
    policy = AutomaticPolicy(daily_launches=1, daily_seconds=25)
    dispatch = DispatchStore(runs.database, runs.clock, policy=policy)
    ticket = dispatch.reserve(60)
    assert runs.get(ticket.run_id)["deadline_at"] == now[0] + 25
    held = view(runs, policy)
    assert (held["held_or_charged_launches"], held["held_or_charged_seconds"]) == (1, 25)
    # The last permitted reservation must still be allowed to start.
    assert dispatch.authorize_launch(ticket)
    runs.started(ticket.run_id)
    now[0] += 4
    assert runs.publish(ticket.run_id, result(runs, ticket.run_id))
    dispatch.reconcile()
    recreated = DispatchStore(runs.database, runs.clock, policy=policy)
    assert recreated.is_automatic(ticket.run_id)
    settled = view(runs, policy)
    assert (settled["held_or_charged_launches"], settled["held_or_charged_seconds"]) == (1, 4)
    assert settled["blocking_reason"] == "daily_budget_exhausted"


def test_prelaunch_failure_refunds_but_unknown_process_keeps_full_hold(client):
    _, runs, _, now = make_case(client)
    policy = AutomaticPolicy(daily_launches=3, daily_seconds=120)
    dispatch = DispatchStore(runs.database, runs.clock, policy=policy)
    ticket = dispatch.reserve(60)
    runs.finish_failed(ticket.run_id, "configuration_error")
    dispatch.reconcile()
    assert view(runs, policy)["held_or_charged_seconds"] == 0
    assert view(runs, policy)["held_or_charged_launches"] == 0
    # Unknown termination keeps status stopping, hence its entire allowance.
    with runs.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE runs SET status='stopping', ended_at=NULL WHERE id=?", (ticket.run_id,)
        )
    now[0] += 100
    assert view(runs, policy)["held_or_charged_seconds"] == 60
    assert view(runs, policy)["held_or_charged_launches"] == 1
    assert dispatch.reserve(60) is None


def test_exhaustion_does_not_create_run_or_consume_message_attempt(client):
    thread, runs, _, now = make_case(client)
    policy = AutomaticPolicy(daily_launches=1, daily_seconds=None)
    dispatch = DispatchStore(runs.database, runs.clock, policy=policy)
    ticket = dispatch.reserve(60)
    assert dispatch.authorize_launch(ticket)
    runs.finish_failed(ticket.run_id, "execution_failed")
    dispatch.reconcile()
    incoming(client, thread, now)
    # First observe the new message, then pass its debounce window.
    from milo_home.wakeup_events import EventPlanner

    EventPlanner(runs.database, runs.clock).plan()
    now[0] += 15
    with pytest.raises(ApiError, match="daily_budget_exhausted"):
        DispatchStore(runs.database, runs.clock, policy=policy).reserve(60)
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM wake_event_delivery").fetchone()[0] == 2


def test_concurrent_reservations_do_not_overbook(client):
    _, runs, _, _ = make_case(client)
    policy = AutomaticPolicy(daily_launches=1, daily_seconds=30)
    dispatch = DispatchStore(runs.database, runs.clock, policy=policy)
    with ThreadPoolExecutor(max_workers=3) as executor:
        tickets = list(executor.map(lambda _: dispatch.reserve(60), range(3)))
    assert sum(ticket is not None for ticket in tickets) == 1
    assert view(runs, policy)["held_or_charged_seconds"] == 30


def test_midnight_clips_timeout_and_clock_rewind_holds_budget(client):
    _, runs, _, now = make_case(client)
    policy = AutomaticPolicy(daily_launches=5, daily_seconds=300)
    day = datetime.fromtimestamp(now[0], BEIJING).date()
    midnight = day_start(day + timedelta(days=1))
    now[0] = midnight - 2
    ScheduleStore(runs.database, runs.clock).override("working", midnight + 60)
    dispatch = DispatchStore(runs.database, runs.clock, policy=policy)
    ticket = dispatch.reserve(60)
    assert runs.get(ticket.run_id)["deadline_at"] == midnight
    assert dispatch.authorize_launch(ticket)
    runs.finish_failed(ticket.run_id, "execution_failed")
    now[0] -= 1
    assert view(runs, policy)["blocking_reason"] == "budget_clock_rewind"
    now[0] = midnight
    assert view(runs, policy)["held_or_charged_launches"] == 0
    assert not dispatch.authorize_launch(ticket)


def test_reduced_policy_prevents_existing_reservation_launch(client):
    _, runs, _, _ = make_case(client)
    original = AutomaticPolicy(daily_launches=2, daily_seconds=120)
    ticket = DispatchStore(runs.database, runs.clock, policy=original).reserve(60)
    smaller = AutomaticPolicy(daily_launches=1, daily_seconds=30)
    assert not DispatchStore(runs.database, runs.clock, policy=smaller).authorize_launch(ticket)
    assert runs.active()["status"] == "stopping"


def test_time_limit_is_charged_after_timeout_and_not_reset_by_recreating_store(client):
    _, runs, _, now = make_case(client)
    policy = AutomaticPolicy(daily_launches=None, daily_seconds=5)
    dispatch = DispatchStore(runs.database, runs.clock, policy=policy)
    ticket = dispatch.reserve(60)
    assert dispatch.authorize_launch(ticket)
    now[0] += 5
    runs.stop(ticket.run_id, "timed_out")
    runs.finish_failed(ticket.run_id, "execution_failed")
    recreated = DispatchStore(runs.database, runs.clock, policy=policy)
    recreated.reconcile()
    report = view(runs, policy)
    assert report["held_or_charged_seconds"] == 5
    assert report["next_timeout"] == 0
    assert report["blocking_reason"] == "daily_budget_exhausted"

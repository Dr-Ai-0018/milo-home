import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_execution_control import legacy, reserve
from test_schedules import timestamp
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.database import Database
from milo_home.errors import ApiError
from milo_home.execution_budget import ExecutionBudget, ExecutionPolicy
from milo_home.execution_control import ExecutionControl
from milo_home.execution_permits import ExecutionPermits


def setup(case, launches=3, seconds=120):
    work, _, _, now = case
    budget = ExecutionBudget(work[0], lambda: now[0])
    budget.configure(ExecutionPolicy(daily_launches=launches, daily_seconds=seconds))
    return budget, ExecutionPermits(work[0], lambda: now[0])


def test_work_requires_explicit_policy_and_time_hold_is_clipped(work_case):
    from test_schedules import EXAMPLE

    from milo_home.schedule_models import SchedulePolicy
    from milo_home.schedules import ScheduleStore

    database, owner, *_ = work_case

    def clock():
        return timestamp("10:00")

    ScheduleStore(database, clock).configure(
        SchedulePolicy.model_validate_json(EXAMPLE.read_bytes())
    )
    task = create(work_case)
    with pytest.raises(ApiError, match="execution_policy_missing"):
        ExecutionControl(database, clock).reserve(owner, task["id"], 60)
    budget = ExecutionBudget(database, clock)
    budget.configure(ExecutionPolicy(daily_launches=1, daily_seconds=7))
    window = ExecutionControl(database, clock).reserve(owner, task["id"], 60)
    assert window["deadline_at"] - window["created_at"] == 7
    assert budget.view()["held_or_charged_seconds"] == 7


def test_each_turn_has_single_use_permit_without_recharging_window_time(execution_case):
    work, control, _, now = execution_case
    budget, permits = setup(execution_case, launches=2, seconds=60)
    task, window = reserve(execution_case)
    extra, _ = work[5].append(work[1], task["id"], "第二轮", str(uuid4()))
    first_input = task["initial_input"]["id"]
    first = permits.reserve(work[1], window["id"], first_input)
    assert permits.reserve(work[1], window["id"], first_input) == first
    assert budget.view()["held_or_charged_launches"] == 1
    # No time remains unreserved, but this permit already holds the window.
    assert permits.consume(work[1], first["id"], first_input)
    assert not permits.consume(work[1], first["id"], first_input)
    with pytest.raises(ApiError, match="turn_busy"):
        permits.reserve(work[1], window["id"], extra["id"])
    assert permits.finish(first["id"], delivery_known=True)
    second = permits.reserve(work[1], window["id"], extra["id"])
    assert permits.consume(work[1], second["id"], extra["id"])
    assert budget.view()["held_or_charged_launches"] == 2
    assert budget.view()["held_or_charged_seconds"] == 60
    now[0] += 4
    assert permits.finish(second["id"], delivery_known=True)
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="completed")
    assert budget.view()["held_or_charged_seconds"] == 4
    assert budget.view()["blocking_reason"] == "daily_budget_exhausted"


def test_second_turn_cannot_bypass_daily_limit(execution_case):
    work, _, _, _ = execution_case
    budget, permits = setup(execution_case, launches=1)
    task, window = reserve(execution_case)
    extra, _ = work[5].append(work[1], task["id"], "不能偷偷继续", str(uuid4()))
    first = permits.reserve(work[1], window["id"], task["initial_input"]["id"])
    assert permits.consume(work[1], first["id"], first["input_id"])
    assert permits.finish(first["id"], delivery_known=True)
    with pytest.raises(ApiError, match="daily_budget_exhausted"):
        permits.reserve(work[1], window["id"], extra["id"])
    assert budget.view()["held_or_charged_launches"] == 1


def test_concurrent_permits_do_not_overbook_and_cancel_cannot_replay(execution_case):
    work, control, _, _ = execution_case
    budget, permits = setup(execution_case, launches=1)
    task, window = reserve(execution_case)
    input_id = task["initial_input"]["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        reservations = list(
            pool.map(lambda _: permits.reserve(work[1], window["id"], input_id), range(4))
        )
    assert len({row["id"] for row in reservations}) == 1
    permit = reservations[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: permits.consume(work[1], permit["id"], input_id), range(4))
        )
    assert sum(results) == 1
    assert not permits.cancel(permit["id"])
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="completed")
    assert budget.view()["held_or_charged_launches"] == 1


def test_unlaunched_window_refunds_and_cancelled_permit_never_reissues(execution_case):
    work, control, _, _ = execution_case
    budget, permits = setup(execution_case, launches=1)
    task, window = reserve(execution_case)
    permit = permits.reserve(work[1], window["id"], task["initial_input"]["id"])
    assert permits.cancel(permit["id"])
    assert not permits.consume(work[1], permit["id"], permit["input_id"])
    assert permits.reserve(work[1], window["id"], permit["input_id"])["state"] == "cancelled"
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="cancelled")
    assert budget.view()["held_or_charged_seconds"] == 0
    assert budget.view()["held_or_charged_launches"] == 0


def test_unknown_delivery_is_not_refunded_by_close_restart_or_backup(execution_case, tmp_path):
    work, control, _, now = execution_case
    budget, permits = setup(execution_case, launches=1, seconds=60)
    task, window = reserve(execution_case)
    permit = permits.reserve(work[1], window["id"], task["initial_input"]["id"])
    assert permits.consume(work[1], permit["id"], permit["input_id"])
    assert not permits.finish(permit["id"], delivery_known=False)
    assert not permits.finish(permit["id"], delivery_known=True)
    assert not control.close(
        window["id"], stop=lambda: True, delivery_known=True, outcome="completed"
    )
    now[0] += 4
    backup = tmp_path / "restored.sqlite3"
    work[0].backup(backup)
    restored = Database(backup)
    restored.initialize()
    view = ExecutionBudget(restored, lambda: now[0]).view()
    assert (view["held_or_charged_launches"], view["held_or_charged_seconds"]) == (1, 60)
    assert not ExecutionPermits(restored, lambda: now[0]).consume(
        work[1], permit["id"], permit["input_id"]
    )


@pytest.mark.parametrize("action", ["pause", "revoke", "reduce", "deadline", "rewind"])
def test_permission_schedule_policy_and_clock_rechecked_before_consume(execution_case, action):
    work, control, schedule, now = execution_case
    budget, permits = setup(execution_case, launches=2, seconds=120)
    task, window = reserve(execution_case)
    permit = permits.reserve(work[1], window["id"], task["initial_input"]["id"])
    if action == "pause":
        schedule.pause(True)
    elif action == "revoke":
        work[4].revoke(work[1])
    elif action == "reduce":
        budget.configure(ExecutionPolicy(daily_launches=1, daily_seconds=1))
    elif action == "deadline":
        now[0] = window["deadline_at"]
    else:
        now[0] -= 1
    if action == "revoke":
        with pytest.raises(ApiError, match="work_not_found"):
            permits.consume(work[1], permit["id"], permit["input_id"])
    else:
        assert not permits.consume(work[1], permit["id"], permit["input_id"])
    assert control.checkpoint(window["id"])["state"] == "stopping"


def test_manual_legacy_uses_shared_hold_and_pre_spawn_marker(execution_case):
    work, control, _, now = execution_case
    budget, _ = setup(execution_case, launches=1, seconds=8)
    thread, runs = legacy(work[0], work[1])
    runs.clock = lambda: now[0]
    run = runs.queue(thread, 60)
    assert runs.get(run)["deadline_at"] == now[0] + 8
    assert runs.authorize_launch(run)
    assert not runs.authorize_launch(run)
    now[0] += 3
    runs.finish_failed(run, "execution_failed")
    assert budget.view()["held_or_charged_seconds"] == 3
    with pytest.raises(ApiError, match="daily_budget_exhausted"):
        control.reserve(work[1], create(work)["id"], 60)
    with pytest.raises(ApiError, match="daily_budget_exhausted"):
        runs.queue(thread, 60)


def test_policy_change_and_rest_block_manual_launch(execution_case):
    work, _, schedule, now = execution_case
    budget, _ = setup(execution_case)
    thread, runs = legacy(work[0], work[1])
    runs.clock = lambda: now[0]
    run = runs.queue(thread, 60)
    budget.configure(ExecutionPolicy(daily_launches=1, daily_seconds=1))
    assert not runs.authorize_launch(run)
    assert runs.get(run)["status"] == "stopping"
    runs.finish_failed(run, "configuration_error")
    assert budget.view()["held_or_charged_launches"] == 0
    budget.configure(ExecutionPolicy(daily_launches=3, daily_seconds=120))
    schedule.override("resting", now[0] + 60)
    with pytest.raises(ApiError, match="work_schedule_closed"):
        runs.queue(thread, 60)


def test_scope_sql_constraints_and_input_mismatch_cannot_consume(execution_case):
    work, _, _, _ = execution_case
    _, permits = setup(execution_case)
    task, window = reserve(execution_case)
    other = create(work)
    permit = permits.reserve(work[1], window["id"], task["initial_input"]["id"])
    assert not permits.consume(work[1], permit["id"], other["initial_input"]["id"])
    with pytest.raises(ApiError, match="work_not_found"):
        permits.reserve(work[2], window["id"], task["initial_input"]["id"])
    with work[0].connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="scope mismatch"):
            connection.execute(
                "INSERT INTO execution_permits VALUES (?,?,?,1,'reserved',NULL,NULL)",
                (str(uuid4()), window["id"], other["initial_input"]["id"]),
            )
    assert permits.consume(work[1], permit["id"], permit["input_id"])
    assert permits.finish(permit["id"], delivery_known=True)
    with work[0].connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="cannot replay"):
            connection.execute(
                "UPDATE execution_permits SET state='reserved',"
                "launched_at=NULL,ended_at=NULL WHERE id=?",
                (permit["id"],),
            )


def test_different_inputs_compete_for_last_turn_and_restart_never_resends(execution_case):
    work, _, _, now = execution_case
    budget, permits = setup(execution_case, launches=1)
    task, window = reserve(execution_case)
    extra, _ = work[5].append(work[1], task["id"], "另一输入", str(uuid4()))

    def claim(input_id):
        try:
            return permits.reserve(work[1], window["id"], input_id)
        except ApiError as error:
            assert error.code == "daily_budget_exhausted"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [task["initial_input"]["id"], extra["id"]]))
    assert sum(row is not None for row in results) == 1
    permit = next(row for row in results if row is not None)
    assert permits.consume(work[1], permit["id"], permit["input_id"])
    assert work[5].get(work[1], task["id"])["status"] == "queued"
    reopened = ExecutionPermits(Database(work[0].path), lambda: now[0])
    assert not reopened.consume(work[1], permit["id"], permit["input_id"])
    assert budget.view()["held_or_charged_launches"] == 1


def test_midnight_clips_shared_hold_and_rewind_cannot_refund(execution_case):
    work, control, schedule, now = execution_case
    budget, permits = setup(execution_case, launches=2, seconds=60)
    midnight = timestamp("00:00", "2026-10-15")
    now[0] = midnight - 2
    schedule.override("working", midnight + 60)
    task, window = reserve(execution_case)
    assert window["deadline_at"] == midnight
    permit = permits.reserve(work[1], window["id"], task["initial_input"]["id"])
    assert permits.consume(work[1], permit["id"], permit["input_id"])
    now[0] -= 1
    assert budget.view()["blocking_reason"] == "budget_clock_rewind"
    assert not permits.finish(permit["id"], delivery_known=True)
    now[0] = midnight
    assert budget.view()["held_or_charged_launches"] == 0
    assert not control.close(
        window["id"], stop=lambda: True, delivery_known=True, outcome="completed"
    )
    with pytest.raises(ApiError, match="run_busy"):
        control.reserve(work[1], create(work)["id"], 60)


def test_known_stop_refunds_only_unconsumed_turns(execution_case):
    work, control, _, _ = execution_case
    budget, permits = setup(execution_case)
    task, window = reserve(execution_case)
    permits.reserve(work[1], window["id"], task["initial_input"]["id"])
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="cancelled")
    assert budget.view()["held_or_charged_launches"] == 0
    task, window = reserve(execution_case)
    permit = permits.reserve(work[1], window["id"], task["initial_input"]["id"])
    assert permits.consume(work[1], permit["id"], permit["input_id"])
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="cancelled")
    assert budget.view()["held_or_charged_launches"] == 1
    assert budget.view()["held_or_charged_seconds"] == 1


def test_clock_rewind_after_later_permit_does_not_release_window(execution_case):
    work, control, _, now = execution_case
    budget, permits = setup(execution_case)
    task, window = reserve(execution_case)
    now[0] += 5
    permit = permits.reserve(work[1], window["id"], task["initial_input"]["id"])
    assert permits.consume(work[1], permit["id"], permit["input_id"])
    now[0] -= 2
    assert now[0] > window["created_at"]
    assert not control.close(
        window["id"], stop=lambda: True, delivery_known=True, outcome="completed"
    )
    assert budget.view()["blocking_reason"] == "budget_clock_rewind"
    assert budget.view()["held_or_charged_seconds"] == 60


@pytest.mark.parametrize("missing_end", [False, True])
def test_legacy_missing_or_reversed_end_keeps_entire_time_hold(execution_case, missing_end):
    work, _, _, now = execution_case
    budget, _ = setup(execution_case)
    thread, runs = legacy(work[0], work[1])
    runs.clock = lambda: now[0]
    run = runs.queue(thread, 60)
    now[0] += 5
    assert runs.authorize_launch(run)
    now[0] -= 2
    runs.finish_failed(run, "execution_failed")
    if missing_end:
        with work[0].connect(write=True) as connection:
            connection.execute("UPDATE runs SET ended_at=NULL WHERE id=?", (run,))
    report = budget.view()
    assert report["held_or_charged_launches"] == 1
    assert report["held_or_charged_seconds"] == 60
    assert report["blocking_reason"] == "budget_clock_rewind"

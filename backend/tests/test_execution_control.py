import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_schedules import EXAMPLE, timestamp
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.database import Database
from milo_home.dispatch import DispatchStore
from milo_home.errors import ApiError
from milo_home.execution_budget import ExecutionBudget, ExecutionPolicy
from milo_home.execution_control import ExecutionControl, execution_busy
from milo_home.runs import RunStore
from milo_home.schedule_models import SchedulePolicy
from milo_home.schedules import ScheduleStore


@pytest.fixture
def execution_case(work_case):
    database = work_case[0]
    now = [timestamp("10:00")]
    schedule = ScheduleStore(database, lambda: now[0])
    schedule.configure(SchedulePolicy.model_validate_json(EXAMPLE.read_bytes()))
    ExecutionBudget(database, lambda: now[0]).configure(
        ExecutionPolicy(daily_launches=None, daily_seconds=None)
    )
    control = ExecutionControl(database, lambda: now[0])
    return work_case, control, schedule, now


def reserve(case, seconds=60):
    work, control, _, _ = case
    task = create(work)
    return task, control.reserve(work[1], task["id"], seconds)


def legacy(database, owner):
    # A synthetic pending legacy letter, using only documented database columns.
    from milo_home.messages import MessageService

    with database.connect() as connection:
        thread = connection.execute("SELECT id FROM threads WHERE owner_id=?", (owner,)).fetchone()[
            0
        ]
    MessageService(database).post(owner, thread, "合成旧留言", str(uuid4()))
    return thread, RunStore(database, lambda: timestamp("10:00"))


def test_unconfigured_resting_paused_and_invalid_windows_refuse(execution_case):
    work, control, schedule, now = execution_case
    task = create(work)
    for seconds in [0, 3601, True, 1.5]:
        with pytest.raises(ValueError):
            control.reserve(work[1], task["id"], seconds)
    for clock in ["08:00", "12:00", "22:00"]:
        now[0] = timestamp(clock)
        with pytest.raises(ApiError, match="work_schedule_closed"):
            control.reserve(work[1], task["id"], 60)
    now[0] = timestamp("10:00")
    schedule.pause(True)
    with pytest.raises(ApiError, match="work_schedule_closed"):
        control.reserve(work[1], task["id"], 60)
    schedule.pause(False)
    with work[0].connect(write=True) as connection:
        connection.execute("UPDATE schedule_state SET policy=NULL")
    with pytest.raises(ApiError, match="work_schedule_closed"):
        control.reserve(work[1], task["id"], 60)


def test_old_and_new_reservations_block_both_service_and_sql(execution_case):
    work, control, _, _ = execution_case
    database, owner, *_ = work
    thread, runs = legacy(database, owner)
    task = create(work)
    old = runs.queue(thread, 60)
    with pytest.raises(ApiError, match="run_busy"):
        control.reserve(owner, task["id"], 60)
    with database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="legacy execution"):
            connection.execute(
                "INSERT INTO execution_windows VALUES (?,?,?,1,61,'held',NULL,NULL)",
                (str(uuid4()), task["id"], "synthetic.service"),
            )
    runs.finish_failed(old, "execution_failed")
    window = control.reserve(owner, task["id"], 60)
    with pytest.raises(ApiError, match="run_busy"):
        runs.queue(thread, 60)
    assert DispatchStore(database).reserve() is None
    with database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="execution already occupied"):
            connection.execute("UPDATE runs SET status='queued' WHERE id=?", (old,))
        with pytest.raises(sqlite3.IntegrityError, match="execution already occupied"):
            connection.execute(
                "INSERT INTO runs (id,thread_id,status,created_at) VALUES (?,?,'queued',1)",
                (str(uuid4()), thread),
            )
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="completed")
    assert runs.queue(thread, 60)


def test_concurrent_legacy_and_work_reservation_has_one_winner(execution_case):
    work, control, _, _ = execution_case
    thread, runs = legacy(work[0], work[1])
    task = create(work)

    def claim(kind):
        try:
            return runs.queue(thread, 60) if kind else control.reserve(work[1], task["id"], 60)
        except ApiError as error:
            assert error.code == "run_busy"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [True, False]))
    assert sum(value is not None for value in results) == 1


@pytest.mark.parametrize("failure", ["stop", "delivery", "exception", "truthy"])
def test_unknown_stays_occupied_across_reopen_and_later_confirmation(execution_case, failure):
    work, control, _, now = execution_case
    task, window = reserve(execution_case)

    def stop():
        if failure == "exception":
            raise RuntimeError("synthetic stop failure")
        return 1 if failure == "truthy" else failure != "stop"

    assert not control.close(
        window["id"], stop=stop, delivery_known=failure != "delivery", outcome="completed"
    )
    reopened = ExecutionControl(Database(work[0].path), lambda: now[0])
    assert reopened.checkpoint(window["id"])["state"] == "unknown"
    assert not reopened.close(
        window["id"], stop=lambda: True, delivery_known=True, outcome="completed"
    )
    assert work[5].get(work[1], task["id"])["status"] == "needs_reconciliation"
    with work[0].connect() as connection:
        assert execution_busy(connection)
    with pytest.raises(ApiError, match="run_busy"):
        control.reserve(work[1], create(work)["id"], 60)


def test_confirmed_close_is_idempotent_and_identity_cannot_change(execution_case):
    work, control, _, _ = execution_case
    task, window = reserve(execution_case)
    calls = []
    with pytest.raises(ApiError, match="window_not_found"):
        control.close(
            str(uuid4()), stop=lambda: calls.append(1), delivery_known=True, outcome="failed"
        )
    assert not calls
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="completed")
    assert control.close(
        window["id"], stop=lambda: calls.append(1), delivery_known=True, outcome="failed"
    )
    assert not calls and work[5].get(work[1], task["id"])["status"] == "completed"
    with work[0].connect(write=True) as connection:
        for sql in ["SET state='held',ended_at=NULL", "SET deadline_at=deadline_at+1"]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE execution_windows {sql} WHERE id=?", (window["id"],))


@pytest.mark.parametrize("action", ["stop", "revoke", "workspace", "pause"])
def test_stop_intent_preserves_reserved_occupancy(execution_case, tmp_path, action):
    work, control, schedule, _ = execution_case
    database, owner, _, workspace, access, tasks, _ = work
    task, window = reserve(execution_case)
    if action == "stop":
        assert tasks.stop(owner, task["id"], str(uuid4()))["status"] == "stopping"
    elif action == "revoke":
        access.revoke(owner)
    elif action == "workspace":
        root = tmp_path / "changed"
        root.mkdir()
        access.register_workspace(workspace, "变更", root)
    else:
        schedule.pause(True)
    assert control.checkpoint(window["id"])["state"] == "stopping"
    with database.connect() as connection:
        assert execution_busy(connection)
        assert connection.execute("SELECT status FROM work_tasks").fetchone()[0] == "stopping"
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="completed")
    with database.connect() as connection:
        assert not execution_busy(connection)
        assert connection.execute("SELECT status FROM work_tasks").fetchone()[0] == "cancelled"


def test_deadline_rest_and_midnight_boundaries(execution_case):
    work, control, schedule, now = execution_case
    _, window = reserve(execution_case)
    now[0] = window["deadline_at"]
    assert control.checkpoint(window["id"])["stop_reason"] == "timed_out"
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="completed")
    now[0] = timestamp("11:59")
    _, window = reserve(execution_case, 3600)
    now[0] = timestamp("12:00")
    assert control.checkpoint(window["id"])["stop_reason"] == "schedule_closed"
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="completed")
    policy = {"windows": [{"start": "00:00", "end": "24:00", "phase": "working"}], "fallback": []}
    schedule.configure(SchedulePolicy.model_validate({"weekdays": policy, "days_off": policy}))
    now[0] = timestamp("23:59")
    _, window = reserve(execution_case, 3600)
    assert window["deadline_at"] == timestamp("00:00", "2026-10-15")
    assert window["deadline_at"] - window["created_at"] == 60


def test_clock_rewind_does_not_release_and_callback_changes_win(execution_case):
    work, control, schedule, now = execution_case
    _, window = reserve(execution_case)
    now[0] -= 1
    assert not control.close(
        window["id"], stop=lambda: True, delivery_known=True, outcome="completed"
    )
    assert control.checkpoint(window["id"])["stop_reason"] == "clock_rewind"
    now[0] += 1
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="completed")
    _, window = reserve(execution_case)

    def stop():
        schedule.override("resting", now[0] + 60)
        return True

    assert control.close(window["id"], stop=stop, delivery_known=True, outcome="completed")
    assert control.checkpoint(window["id"])["stop_reason"] == "schedule_closed"


def test_ready_automatic_dispatch_waits_without_spending_attempts(client, tmp_path):
    from test_dispatch import make_case

    from milo_home.work_access import WorkAccess
    from milo_home.work_tasks import WorkTasks

    thread, runs, dispatch, now = make_case(client)
    database = runs.database
    with database.connect() as connection:
        owner = connection.execute("SELECT owner_id FROM threads WHERE id=?", (thread,)).fetchone()[
            0
        ]
    workspace = str(uuid4())
    access = WorkAccess(database, lambda: now[0])
    access.register_workspace(workspace, "合成自动派发竞争", tmp_path)
    access.grant(owner)
    tasks = WorkTasks(database, lambda: now[0])
    session, _ = tasks.main_session(owner, workspace)
    task, _ = tasks.create(owner, session["id"], "合成工作", str(uuid4()))
    ExecutionBudget(database, lambda: now[0]).configure(
        ExecutionPolicy(daily_launches=None, daily_seconds=None)
    )
    control = ExecutionControl(database, lambda: now[0])
    window = control.reserve(owner, task["id"], 60)
    assert dispatch.reserve() is None
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM wake_event_delivery").fetchone()[0] == 0
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="completed")
    assert dispatch.reserve() is not None
    with database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="legacy execution"):
            connection.execute(
                "UPDATE execution_windows SET state='held',ended_at=NULL WHERE id=?",
                (window["id"],),
            )


@pytest.mark.parametrize("action", ["stop", "revoke", "pause"])
def test_later_stop_intents_keep_unknown_task_visible(execution_case, action):
    work, control, schedule, _ = execution_case
    task, window = reserve(execution_case)
    assert not control.close(
        window["id"], stop=lambda: False, delivery_known=True, outcome="failed"
    )
    if action == "stop":
        work[5].stop(work[1], task["id"], str(uuid4()))
    elif action == "revoke":
        work[4].revoke(work[1])
    else:
        schedule.pause(True)
    assert control.checkpoint(window["id"])["state"] == "unknown"
    with work[0].connect() as connection:
        assert (
            connection.execute("SELECT status FROM work_tasks").fetchone()[0]
            == "needs_reconciliation"
        )


def test_backup_restore_keeps_unknown_window_and_blocks_both_entries(execution_case, tmp_path):
    work, control, _, now = execution_case
    _, window = reserve(execution_case)
    assert not control.close(
        window["id"], stop=lambda: False, delivery_known=False, outcome="failed"
    )
    backup = tmp_path / "restored-window.sqlite3"
    work[0].backup(backup)
    restored = Database(backup)
    restored.initialize()
    restored_control = ExecutionControl(restored, lambda: now[0])
    assert restored_control.checkpoint(window["id"])["state"] == "unknown"
    with restored.connect() as connection:
        assert execution_busy(connection)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    thread, runs = legacy(restored, work[1])
    with pytest.raises(ApiError, match="run_busy"):
        runs.queue(thread, 60)
    assert not restored_control.close(
        window["id"], stop=lambda: True, delivery_known=True, outcome="completed"
    )

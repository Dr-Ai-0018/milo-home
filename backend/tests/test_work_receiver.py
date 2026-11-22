import json
import os
import signal
import sys
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_work_cli import runtime_case as runtime_case
from test_work_coordinator import starts
from test_work_execution import counts
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.cli import main
from milo_home.codex_rpc import ProtocolError
from milo_home.errors import ApiError
from milo_home.execution_lock import execution_lock
from milo_home.scheduler_cli import StopRequest
from milo_home.work_receiver import ReceiveLimit, WorkReceiver
from milo_home.work_runtime import WorkRuntime

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root receiver")


def receiver_for(launch_case, *, seconds=60):
    _, runtime, _, launcher, _, _ = launch_case
    stop = StopRequest()
    launcher.stop_requested = stop.is_set
    return WorkReceiver(launcher, stop, ReceiveLimit(seconds, runtime.clock))


def receive(launch_case, receiver=None):
    case = launch_case[0][0]
    receiver = receiver or receiver_for(launch_case)
    return receiver.run(case[1], case[-1]["id"], credential="synthetic-secret")


def invoke(
    monkeypatch, launch_case, *, timeout=60, interval=2, account=None, once=False, credential=None
):
    case, runtime, journal, launcher, _, _ = launch_case
    monkeypatch.setenv("MILO_DATABASE", str(runtime.database.path))
    monkeypatch.setenv("SYNTHETIC_KEY", "synthetic-secret")
    monkeypatch.setattr("milo_home.work_execution.NewawzConfig.load", lambda: launcher.config)
    monkeypatch.setattr(
        "milo_home.work_execution.WorkRuntime",
        lambda database, journal: WorkRuntime(database, journal, runtime.clock),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "milo-home",
            *(["--database-user", account] if account else []),
            "--run-directory",
            str(journal.path.parent),
            "work",
            "take-next" if once else "listen",
            "--owner",
            case[0][1],
            "--session",
            case[0][-1]["id"],
            *([] if timeout is None else ["--timeout", str(timeout)]),
            *([] if once else ["--interval", str(interval)]),
            "--execute",
            *(["--credential-file", str(credential)] if credential else []),
        ],
    )
    main()


def test_cli_takes_oldest_uncancelled_task_and_exits_after_one(
    launch_case, fake_units, monkeypatch, capsys
):
    case, runtime, journal, _, output, _ = launch_case
    cancelled = create(case[0])
    case[0][5].stop(case[0][1], cancelled["id"], str(uuid4()))
    first = create(case[0])
    second = create(case[0])
    invoke(monkeypatch, launch_case)
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "completed" and report["task_id"] == first["id"]
    assert counts(runtime.database) == [1, 1, 1]
    assert case[0][5].get(case[0][1], second["id"])["status"] == "queued"
    assert {request["threadId"] for request in starts(output)} == {"synthetic-main"}
    assert not journal.outstanding() and not list(journal.path.parent.glob("*.env"))
    assert fake_units[1][0].process.poll() is not None


def test_arriving_task_is_seen_without_holding_idle_lock(launch_case, fake_units, monkeypatch):
    case, runtime, _, _, output, _ = launch_case
    receiver = receiver_for(launch_case)
    arrived = []

    def arrive(seconds):
        with execution_lock(runtime.database):
            arrived.append(create(case[0]))

    monkeypatch.setattr(receiver.stop, "wait", arrive)
    result = receive(launch_case, receiver)
    assert result.task_id == arrived[0]["id"] and result.outcome == "completed"
    assert len(starts(output)) == 1


def test_timer_idle_then_one_task_gets_full_default_window(
    launch_case, fake_units, monkeypatch, capsys
):
    case, runtime, _, launcher, output, _ = launch_case
    from milo_home.execution_budget import ExecutionBudget, ExecutionPolicy

    ExecutionBudget(runtime.database, runtime.clock).configure(
        ExecutionPolicy(daily_launches=200, daily_seconds=None)
    )
    invoke(monkeypatch, launch_case, timeout=None, once=True)
    assert json.loads(capsys.readouterr().out)["outcome"] == "queue_empty"
    assert counts(runtime.database) == [0, 0, 0] and not fake_units[1]

    create(case[0])
    second = create(case[0])
    prepare = launcher._prepare

    def later(self, credential, *, execute):
        case[-1][0] += 30
        return prepare(credential, execute=execute)

    monkeypatch.setattr(type(launcher), "_prepare", later)
    invoke(monkeypatch, launch_case, timeout=None, once=True)
    assert json.loads(capsys.readouterr().out) == {"outcome": "completed", "turns": 1}
    with runtime.database.connect() as connection:
        created, deadline = connection.execute(
            "SELECT created_at,deadline_at FROM execution_windows"
        ).fetchone()
    assert deadline - created == 3600
    assert len(starts(output)) == 1
    assert case[0][5].get(case[0][1], second["id"])["status"] == "queued"


@pytest.mark.parametrize("blocker", ["pause", "worker_busy", "run_busy"])
def test_timer_skips_pause_and_occupancy(launch_case, fake_units, monkeypatch, capsys, blocker):
    case, runtime, _, _, _, _ = launch_case
    task = create(case[0])
    if blocker == "worker_busy":
        with execution_lock(runtime.database):
            invoke(monkeypatch, launch_case, once=True)
    else:
        if blocker == "pause":
            case[2].pause(True)
        else:
            from milo_home.execution_control import ExecutionControl

            ExecutionControl(runtime.database, runtime.clock).reserve(case[0][1], task["id"], 60)
        invoke(monkeypatch, launch_case, once=True)
    expected = "work_schedule_closed" if blocker == "pause" else blocker
    assert json.loads(capsys.readouterr().out)["outcome"] == expected
    assert not fake_units[1]


@pytest.mark.parametrize("action", ["expire", "signal"])
def test_idle_exit_never_launches_or_cancels_arriving_task(
    launch_case, fake_units, monkeypatch, action
):
    case, runtime, _, _, output, _ = launch_case
    receiver = receiver_for(launch_case)
    arrived = []

    def finish(seconds):
        arrived.append(create(case[0]))
        if action == "expire":
            case[-1][0] += 60
        else:
            receiver.stop.set()

    monkeypatch.setattr(receiver.stop, "wait", finish)
    result = receive(launch_case, receiver)
    assert result.outcome == ("wait_expired" if action == "expire" else "stop_requested")
    assert case[0][5].get(case[0][1], arrived[0]["id"])["status"] == "queued"
    assert counts(runtime.database) == [0, 0, 0] and not output.exists() and not fake_units[1]


@pytest.mark.parametrize("change", ["regrant", "workspace", "database", "private", "clock"])
def test_idle_cannot_change_original_identity_or_authorization(
    launch_case, fake_units, monkeypatch, tmp_path, change
):
    case, runtime, journal, _, output, _ = launch_case
    receiver = receiver_for(launch_case)

    def alter(seconds):
        if change == "regrant":
            case[0][4].revoke(case[0][1])
            case[0][4].grant(case[0][1])
        elif change == "workspace":
            case[0][4].register_workspace(case[0][3], "合成", tmp_path, enabled=False)
        elif change == "database":
            replacement = runtime.database.path.with_name("replacement.sqlite3")
            runtime.database.backup(replacement)
            os.replace(replacement, runtime.database.path)
        elif change == "private":
            with journal._connect() as connection:
                connection.execute("DELETE FROM business_binding")
        else:
            case[-1][0] -= 1

    monkeypatch.setattr(receiver.stop, "wait", alter)
    with pytest.raises((ProtocolError, ApiError)):
        receive(launch_case, receiver)
    assert counts(runtime.database) == [0, 0, 0] and not output.exists() and not fake_units[1]


@pytest.mark.parametrize("blocker", ["schedule", "policy", "budget", "unknown", "private"])
def test_idle_blockers_fail_without_model_or_budget_reservation(launch_case, fake_units, blocker):
    case, runtime, journal, _, output, _ = launch_case
    if blocker == "schedule":
        case[2].pause(True)
    elif blocker == "private":
        journal.open_stream("milo-work-" + "d" * 32 + ".service")
    else:
        from milo_home.execution_control import ExecutionControl

        if blocker in {"budget", "unknown"}:
            task = create(case[0])
            window = ExecutionControl(runtime.database, runtime.clock).reserve(
                case[0][1], task["id"], 60
            )
            if blocker == "budget":
                with runtime.database.connect(write=True) as connection:
                    connection.execute(
                        "UPDATE execution_windows SET state='closed',ended_at=created_at"
                    )
                    connection.execute(
                        "UPDATE execution_policy SET policy=?",
                        ('{"daily_launches":1,"daily_seconds":1}',),
                    )
                    # Budget is used only when a permit proves a launch.
                    connection.execute(
                        "INSERT INTO execution_permits VALUES (?,?,?,?,'finished',?,?)",
                        (
                            str(uuid4()),
                            window["id"],
                            task["initial_input"]["id"],
                            case[-1][0],
                            case[-1][0],
                            case[-1][0],
                        ),
                    )
            else:
                with runtime.database.connect(write=True) as connection:
                    connection.execute("UPDATE execution_windows SET state='unknown'")
        else:
            with runtime.database.connect(write=True) as connection:
                connection.execute("UPDATE execution_policy SET policy=NULL")
    before = counts(runtime.database)
    with pytest.raises((ApiError, ProtocolError)):
        receive(launch_case)
    assert counts(runtime.database) == before and not output.exists() and not fake_units[1]


def test_preparation_cannot_restart_expired_receiver_budget(launch_case, fake_units, monkeypatch):
    case, runtime, _, launcher, output, _ = launch_case
    task = create(case[0])
    original = launcher._prepare

    def prepare(credential, *, execute):
        result = original(credential, execute=execute)
        case[-1][0] += 61
        return result

    monkeypatch.setattr(launcher, "_prepare", prepare)
    assert receive(launch_case).outcome == "wait_expired"
    assert counts(runtime.database) == [0, 0, 0] and not fake_units[1] and not output.exists()
    assert case[0][5].get(case[0][1], task["id"])["status"] == "queued"


def test_receiver_lock_conflict_is_not_retried(launch_case, fake_units):
    runtime = launch_case[1]
    with execution_lock(runtime.database), pytest.raises(ApiError, match="worker_busy"):
        receive(launch_case)
    assert counts(runtime.database) == [0, 0, 0] and not fake_units[1]


def test_cli_idle_sigterm_restores_handler_without_spawning(
    launch_case, fake_units, monkeypatch, capsys
):
    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(StopRequest, "wait", lambda *args: os.kill(os.getpid(), signal.SIGTERM))
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, launch_case)
    assert exit_.value.code == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "stop_requested"
    assert signal.getsignal(signal.SIGTERM) == previous and not fake_units[1]


@pytest.mark.parametrize("timeout,interval", [(0, 2), (3601, 2), (60, 0), (60, 31)])
def test_cli_bad_limits_never_reserve(launch_case, fake_units, monkeypatch, timeout, interval):
    create(launch_case[0][0])
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, launch_case, timeout=timeout, interval=interval)
    assert exit_.value.code == 1 and counts(launch_case[1].database) == [0, 0, 0]


def test_original_monotonic_limit_survives_preparation(launch_case, fake_units):
    create(launch_case[0][0])
    receiver = receiver_for(launch_case, seconds=60)
    result = receive(launch_case, receiver)
    assert result.outcome == "completed"
    assert fake_units[1][0].deadline <= receiver.limit.monotonic_deadline


def test_limit_uses_both_clocks_and_does_not_round_up():
    wall, mono = [100.2], [50.2]
    limit = ReceiveLimit(10, lambda: wall[0], lambda: mono[0])
    mono[0] += 4.5
    assert limit.seconds() == 5
    wall[0] += 7
    assert limit.seconds() == 3
    wall[0] -= 1
    with pytest.raises(ProtocolError, match="backwards"):
        limit.remaining()

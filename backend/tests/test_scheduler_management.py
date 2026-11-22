import json
import signal
import sqlite3
import subprocess
import sys

import pytest
from test_dispatch import make_case
from test_worker import fake_runtime

from milo_home import worker as module
from milo_home.automatic_budget import AutomaticPolicy
from milo_home.database import Database
from milo_home.run_transport import EventStream
from milo_home.scheduler import Scheduler
from milo_home.scheduler_cli import StopRequest, stop_signals
from milo_home.worker import Worker


def private_policy(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "daily_launches": 2,
                "daily_seconds": 120,
            }
        )
    )
    policy.chmod(0o600)
    return policy


def command(database, *arguments):
    return subprocess.run(
        [sys.executable, "-m", "milo_home.cli", "scheduler", *arguments],
        env={
            "PATH": "/usr/bin:/bin",
            "MILO_DATABASE": str(database),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_check_is_readonly_and_reports_explicit_policy(tmp_path):
    database = Database(tmp_path / "home.sqlite3")
    database.initialize()
    process = command(database.path, "check", "--policy", str(private_policy(tmp_path)))
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["automatic_wakeup"] is False
    assert report["budget"]["remaining_launches"] == 2
    assert report["budget"]["next_timeout"] <= 120
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert (
            connection.execute("SELECT last_scan_at FROM wake_event_cursor").fetchone()[0] is None
        )


def test_execution_without_explicit_flag_rejected_before_opening_files(tmp_path):
    missing = tmp_path / "not-created.sqlite3"
    process = command(
        missing, "run", "--policy", str(tmp_path / "not-created.json"), "--cycles", "1"
    )
    assert process.returncode != 0
    assert "Configuration or file check failed" in process.stderr
    assert not missing.exists()


def test_missing_or_old_database_not_created_or_migrated(tmp_path):
    policy = private_policy(tmp_path)
    path = tmp_path / "old.sqlite3"
    assert command(path, "check", "--policy", str(policy)).returncode != 0
    assert not path.exists()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=4")
    path.chmod(0o600)
    before = path.read_bytes()
    assert command(path, "check", "--policy", str(policy)).returncode != 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signals_set_stop_flag_and_restore_previous_handlers(signum):
    stop = StopRequest()
    previous = signal.getsignal(signum)
    with stop_signals(stop):
        signal.raise_signal(signum)
        assert stop.is_set()
    assert signal.getsignal(signum) == previous


def test_inflight_signal_prevents_publication_and_next_cycle(client, monkeypatch):
    _, runs, _, _ = make_case(client)
    stop = StopRequest()
    policy = AutomaticPolicy(daily_launches=3, daily_seconds=180)
    worker = Worker(runs.database, clock=runs.clock, policy=policy, stop_requested=stop.is_set)
    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)

    def execute(command, prompt, store, run_id):
        store.started(run_id)
        signal.raise_signal(signal.SIGTERM)
        events = EventStream()
        events.completed = 1
        return 0, events, True

    monkeypatch.setattr(module, "execute", execute)
    with stop_signals(stop):
        report = Scheduler(worker, timeout=60).run(cycles=3, stop=stop)
    assert (report.cycles, report.reason) == (1, "run_cancelled")
    assert worker.store.active() is None
    assert client.get("/api/threads").json()[0]["pending_count"] == 2
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_stop_during_preparation_never_launches(client, monkeypatch):
    _, runs, _, _ = make_case(client)
    stop = StopRequest()
    worker = Worker(runs.database, clock=runs.clock, stop_requested=stop.is_set)

    def prepare(*arguments):
        root = fake_runtime(*arguments)
        stop.set()
        return root

    def forbidden(*arguments):
        raise AssertionError("No execution after stop in preparation")

    monkeypatch.setattr(module, "prepare", prepare)
    monkeypatch.setattr(module, "execute", forbidden)
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)
    report = Scheduler(worker).run(cycles=3, stop=stop)
    assert report.reason == "run_cancelled"
    assert runs.active() is None
    with runs.database.connect() as connection:
        assert connection.execute("SELECT launched_at FROM wake_dispatches").fetchone()[0] is None


def test_managed_loop_exits_when_next_batch_would_exceed_daily_limit(client, monkeypatch):
    from test_automatic_budget import incoming
    from test_automatic_worker import successful_execute
    from test_scheduler import SimulatedStop

    from milo_home.wakeup_events import EventPlanner

    thread, runs, _, now = make_case(client)
    policy = AutomaticPolicy(daily_launches=1, daily_seconds=None)
    worker = Worker(runs.database, clock=runs.clock, policy=policy)
    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "execute", successful_execute)
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)
    added = False

    def wait(seconds):
        nonlocal added
        now[0] += seconds
        if not added:
            incoming(client, thread, now)
            EventPlanner(runs.database, runs.clock).plan()
            added = True

    report = Scheduler(worker, timeout=60).run(cycles=4, stop=SimulatedStop(wait))
    assert report.succeeded == 1
    assert report.reason == "daily_budget_exhausted"
    assert client.get("/api/threads").json()[0]["pending_count"] == 1

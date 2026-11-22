from threading import Event

import pytest
from test_automatic_worker import successful_execute
from test_dispatch import make_case
from test_runs import pending
from test_worker import fake_runtime

from milo_home import worker as module
from milo_home.errors import ApiError
from milo_home.run_transport import EventStream
from milo_home.scheduler import Scheduler
from milo_home.schedules import ScheduleStore
from milo_home.worker import Worker, worker_lock


class SimulatedStop:
    def __init__(self, wait):
        self.wait_action = wait
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        self.wait_action(seconds)
        return self.stopped


def synthetic_worker(runs, monkeypatch):
    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "execute", successful_execute)
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)
    return Worker(runs.database, clock=runs.clock)


def test_multiple_cycles_publish_once_then_idle(client, monkeypatch):
    _, runs, _, now = make_case(client)
    worker = synthetic_worker(runs, monkeypatch)
    stop = SimulatedStop(lambda seconds: now.__setitem__(0, now[0] + seconds))
    report = Scheduler(worker, timeout=60).run(cycles=3, stop=stop)
    assert (report.cycles, report.succeeded, report.idle) == (3, 1, 2)
    assert report.reason == "cycles_exhausted"
    assert stop.waits == [15, 15]
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM messages WHERE role='milo'").fetchone()[0] == 2
        )
    assert runs.active() is None


def test_resting_collects_then_resumes_without_losing_messages(client, monkeypatch):
    _, runs, _, now = make_case(client)
    worker = synthetic_worker(runs, monkeypatch)
    schedule = ScheduleStore(runs.database, runs.clock)
    schedule.override("resting", now[0] + 60)

    def resume(seconds):
        assert runs.active() is None
        assert client.get("/api/threads").json()[0]["pending_count"] == 2
        now[0] += seconds
        schedule.override(None, None)

    report = Scheduler(worker, timeout=60).run(cycles=2, stop=SimulatedStop(resume))
    assert (report.idle, report.succeeded) == (1, 1)
    assert client.get("/api/threads").json()[0]["pending_count"] == 0


def test_unconfigured_schedule_never_launches(client, monkeypatch):
    _, runs = pending(client, 1)
    worker = synthetic_worker(runs, monkeypatch)

    def forbidden(*args):
        raise AssertionError("Unconfigured schedule must not launch")

    monkeypatch.setattr(module, "prepare", forbidden)
    report = Scheduler(worker).run(cycles=2, stop=SimulatedStop(lambda _: None))
    assert report.idle == 2
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM wake_events").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_busy_worker_is_waited_for_without_touching_occupancy(client, monkeypatch):
    _, runs, _, _ = make_case(client)
    worker = synthetic_worker(runs, monkeypatch)
    stop = SimulatedStop(lambda _: None)
    with worker_lock(runs.database):
        report = Scheduler(worker).run(cycles=2, stop=stop)
    assert (report.cycles, report.busy, report.succeeded) == (2, 2, 0)
    assert stop.waits == [15]
    assert runs.active() is None


def test_preexisting_stop_never_reads_database():
    stop = Event()
    stop.set()
    report = Scheduler(None).run(cycles=3, stop=stop)
    assert report.cycles == 0
    assert report.reason == "stop_requested"


def test_stop_interrupts_wait_without_another_cycle(client, monkeypatch):
    _, runs, _, _ = make_case(client)
    worker = synthetic_worker(runs, monkeypatch)
    stop = SimulatedStop(lambda _: setattr(stop, "stopped", True))
    report = Scheduler(worker, timeout=60).run(cycles=3, stop=stop)
    assert (report.cycles, report.succeeded, report.reason) == (1, 1, "stop_requested")
    assert stop.waits == [15]


def test_failed_execution_stops_loop_without_retry(client, monkeypatch):
    _, runs, _, _ = make_case(client)
    worker = synthetic_worker(runs, monkeypatch)
    monkeypatch.setattr(module, "execute", lambda *args: (1, EventStream(), True))
    stop = SimulatedStop(lambda _: pytest.fail("Failure must not enter another wait"))
    report = Scheduler(worker).run(cycles=3, stop=stop)
    assert (report.cycles, report.reason) == (1, "run_failed")
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert runs.active() is None


def test_unknown_termination_exits_with_occupancy_retained(client, monkeypatch):
    _, runs, _, _ = make_case(client)
    worker = synthetic_worker(runs, monkeypatch)
    monkeypatch.setattr(module, "execute", lambda *args: (0, EventStream(), False))
    stop = SimulatedStop(lambda _: pytest.fail("Unknown termination must exit"))
    with pytest.raises(ApiError, match="termination_unknown"):
        Scheduler(worker).run(cycles=3, stop=stop)
    assert runs.active()["status"] == "stopping"
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


@pytest.mark.parametrize("options", [{"interval": 0}, {"interval": True}, {"timeout": 3601}])
def test_invalid_settings_fail_before_worker_access(options):
    with pytest.raises(ValueError):
        Scheduler(None, **options)


@pytest.mark.parametrize("cycles", [0, -1, True, 1.5])
def test_explicit_positive_cycles_required(cycles):
    with pytest.raises(ValueError):
        Scheduler(None).run(cycles=cycles, stop=Event())

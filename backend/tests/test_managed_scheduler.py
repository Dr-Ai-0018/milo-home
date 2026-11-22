from test_automatic_worker import successful_execute
from test_dispatch import make_case
from test_management_lease import POLICY, view
from test_worker import fake_runtime

from milo_home import worker as module
from milo_home.managed_stop import ManagedStop
from milo_home.management_lease import ManagementLease
from milo_home.scheduler import Scheduler
from milo_home.scheduler_cli import StopRequest
from milo_home.worker import Worker


def setup_managed(runs, now):
    lease = ManagementLease(runs.database, POLICY, clock=runs.clock, monotonic=runs.clock)
    lease.claim()
    stop = StopRequest()
    managed = ManagedStop(stop, lease, monotonic=runs.clock)
    worker = Worker(
        runs.database,
        clock=runs.clock,
        policy=POLICY,
        stop_requested=stop.is_set,
        progress=managed.progress,
    )
    return lease, stop, managed, worker


def test_idle_wait_renews_without_threads_or_extra_cycles(client, monkeypatch):
    _, runs, _, now = make_case(client)
    lease, stop, managed, worker = setup_managed(runs, now)
    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "execute", successful_execute)
    monkeypatch.setattr(module, "stop_unit", lambda _: True)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        now[0] += seconds
        return False

    monkeypatch.setattr(stop, "wait", wait)
    report = Scheduler(worker, interval=60, timeout=60).run(cycles=2, stop=managed)
    assert (report.cycles, report.succeeded, report.idle) == (2, 1, 1)
    assert waits == [15, 15, 15, 15]
    assert view(runs.database, now[0]) == ("enabled", "management_lease_current", now[0] + 45)
    lease.close()
    assert view(runs.database, now[0])[0] == "stopped"
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM wake_dispatches").fetchone()[0] == 1


def test_inflight_checkpoints_renew_management_without_touching_start_permission(
    client, monkeypatch
):
    _, runs, _, now = make_case(client)
    lease, _, managed, worker = setup_managed(runs, now)
    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "stop_unit", lambda _: True)

    def execute(command, prompt, store, run_id):
        result = successful_execute(command, prompt, store, run_id)
        with runs.database.connect() as connection:
            before = dict(connection.execute("SELECT * FROM wake_dispatches").fetchone())
        for _ in range(3):
            now[0] += 15
            assert store.checkpoint(run_id)["status"] == "running"
            assert view(runs.database, now[0])[2] == now[0] + 45
        with runs.database.connect() as connection:
            assert dict(connection.execute("SELECT * FROM wake_dispatches").fetchone()) == before
        return result

    monkeypatch.setattr(module, "execute", execute)
    report = Scheduler(worker, timeout=60).run(cycles=1, stop=managed)
    assert report.succeeded == 1
    lease.close()


def test_lost_lease_cancels_inflight_result_and_prevents_next_cycle(client, monkeypatch):
    _, runs, _, now = make_case(client)
    lease, stop, managed, worker = setup_managed(runs, now)
    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "stop_unit", lambda _: True)

    def execute(command, prompt, store, run_id):
        result = successful_execute(command, prompt, store, run_id)
        now[0] += 45
        assert store.checkpoint(run_id)["stop_reason"] == "cancelled"
        return result

    monkeypatch.setattr(module, "execute", execute)
    report = Scheduler(worker, timeout=90).run(cycles=3, stop=managed)
    assert (report.cycles, report.reason) == (1, "run_cancelled")
    assert lease.lost and stop.is_set()
    assert runs.active() is None
    assert client.get("/api/threads").json()[0]["pending_count"] == 2
    lease.close()
    assert view(runs.database, now[0])[0] == "unknown"


def test_stop_during_idle_does_not_renew_or_enter_next_cycle(client, monkeypatch):
    _, runs, _, now = make_case(client)
    lease, stop, managed, worker = setup_managed(runs, now)
    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "execute", successful_execute)
    monkeypatch.setattr(module, "stop_unit", lambda _: True)
    before = now[0]

    def wait(seconds):
        now[0] += 16
        stop.set()
        return True

    monkeypatch.setattr(stop, "wait", wait)
    report = Scheduler(worker, interval=60, timeout=60).run(cycles=3, stop=managed)
    assert report.reason == "stop_requested" and report.cycles == 1
    assert view(runs.database, now[0])[2] == before + 45
    lease.close()

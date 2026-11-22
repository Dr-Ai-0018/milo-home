import os
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from milo_home.database import Database
from milo_home.dispatch import DispatchStore
from milo_home.run_environment import prepare, unit_command
from milo_home.run_transport import execute, stop_unit, unit_empty
from milo_home.runs import RunStore
from milo_home.schedule_models import SchedulePolicy
from milo_home.schedules import ScheduleStore
from milo_home.wakeup_events import EventPlanner
from milo_home.worker import Worker

pytestmark = pytest.mark.skipif(
    os.getuid() != 0 or not Path("/run/systemd/system").exists(),
    reason="Requires local root/systemd for real process-tree verification",
)


def setup_run(tmp_path, monkeypatch, timeout, *, automatic=False):
    database = Database(tmp_path / "process.sqlite3")
    database.initialize()
    thread = str(uuid4())
    owner = str(uuid4())
    with database.connect(write=True) as connection:
        connection.execute("INSERT INTO users VALUES (?, 'test', '访客', 'synthetic', 0)", (owner,))
        connection.execute("INSERT INTO threads VALUES (?, ?, 0)", (thread, owner))
        connection.execute(
            "INSERT INTO messages (id, thread_id, sequence, author_id, role, body, client_key, "
            "created_at) VALUES (?, ?, 1, ?, 'visitor', '进程树合成留言', ?, 0)",
            (str(uuid4()), thread, owner, str(uuid4())),
        )
    profile = tmp_path / "profile.toml"
    profile.write_text("""model = "test-model"
model_provider = "fixture"
[model_providers.fixture]
name = "fixture"
base_url = "https://example.invalid"
env_key = "MILO_PROCESS_TEST_KEY"
wire_api = "responses"
""")
    monkeypatch.setenv("MILO_PROCESS_TEST_KEY", "synthetic-only")
    store = RunStore(database)
    if automatic:
        day = {
            "windows": [
                {
                    "start": "00:00",
                    "end": "24:00",
                    "phase": "working",
                }
            ],
            "fallback": [],
        }
        ScheduleStore(database).configure(SchedulePolicy(weekdays=day, days_off=day))
        EventPlanner(database, lambda: time.time() - 16).plan()
        dispatch = DispatchStore(database)
        ticket = dispatch.reserve(timeout)
        assert ticket and dispatch.authorize_launch(ticket)
        run = ticket.run_id
    else:
        run = store.queue(thread, timeout)
    base = tmp_path / "runs" / run
    root = prepare(base, store.snapshot(run), profile)
    return store, run, root


def has_sleep(unit):
    result = subprocess.run(
        ["systemctl", "show", unit, "-p", "ControlGroup", "--value"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if not result.stdout.strip():
        return False
    group = Path("/sys/fs/cgroup") / result.stdout.strip().lstrip("/")
    if not group.exists():
        return False
    for file in group.rglob("cgroup.procs"):
        for pid in file.read_text().split():
            comm = Path("/proc") / pid / "comm"
            if comm.exists() and comm.read_text().strip() == "sleep":
                return True
    return False


SLEEP_CODE = (
    "import json,subprocess,time; "
    "child=subprocess.Popen(['/usr/bin/sleep','120']); "
    "print(json.dumps({'type':'turn.started'}),flush=True); child.wait()"
)


@pytest.mark.parametrize("cancel", [True, False])
def test_real_cancel_and_timeout_end_the_child_tree(tmp_path, monkeypatch, cancel):
    store, run, root = setup_run(tmp_path, monkeypatch, 15 if cancel else 3)
    unit = store.get(run)["unit_name"]
    observed = threading.Event()

    def observe():
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if has_sleep(unit):
                observed.set()
                if cancel:
                    store.stop(run)
                return
            time.sleep(0.05)

    observer = threading.Thread(target=observe)
    observer.start()
    try:
        command = unit_command(unit, root, 20, ["/usr/bin/python3", "-c", SLEEP_CODE])
        code, events, ended = execute(command, root.parent / "prompt.txt", store, run)
        observer.join(timeout=10)
        assert observed.is_set(), "A real child must be observed before stop"
        assert ended and unit_empty(unit) and not has_sleep(unit)
        assert events.completed == 0
        store.finish_failed(run, "test_failure")
        assert store.get(run)["status"] == ("cancelled" if cancel else "timed_out")
    finally:
        stop_unit(unit)
        (root.parent / "credential.env").unlink(missing_ok=True)


def test_recovery_stops_a_real_orphan_before_releasing(tmp_path, monkeypatch):
    store, run, root = setup_run(tmp_path, monkeypatch, 60)
    unit = store.get(run)["unit_name"]
    command = unit_command(unit, root, 30, ["/usr/bin/python3", "-c", SLEEP_CODE])
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not has_sleep(unit) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert has_sleep(unit)
        store.started(run)
        assert Worker(store.database).recover() == run
        assert store.active() is None and unit_empty(unit) and not has_sleep(unit)
        assert store.get(run)["status"] == "failed"
    finally:
        stop_unit(unit)
        process.wait(timeout=5)


def test_schedule_pause_stops_real_child_and_prevents_publication(tmp_path, monkeypatch):
    store, run, root = setup_run(tmp_path, monkeypatch, 30)
    unit = store.get(run)["unit_name"]
    observed = threading.Event()

    def pause_after_child_starts():
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if has_sleep(unit):
                observed.set()
                ScheduleStore(store.database).pause(True)
                return
            time.sleep(0.05)

    observer = threading.Thread(target=pause_after_child_starts)
    observer.start()
    try:
        command = unit_command(unit, root, 30, ["/usr/bin/python3", "-c", SLEEP_CODE])
        code, events, ended = execute(command, root.parent / "prompt.txt", store, run)
        observer.join(timeout=10)
        assert observed.is_set() and ended and not has_sleep(unit)
        assert events.completed == 0
        store.finish_failed(run, "execution_failed")
        assert store.get(run)["status"] == "cancelled"
    finally:
        stop_unit(unit)
        (root.parent / "credential.env").unlink(missing_ok=True)


def test_automatic_rest_boundary_ends_a_real_child_tree(tmp_path, monkeypatch):
    store, run, root = setup_run(tmp_path, monkeypatch, 30, automatic=True)
    unit = store.get(run)["unit_name"]
    observed = threading.Event()

    def rest_after_child_starts():
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if has_sleep(unit):
                observed.set()
                ScheduleStore(store.database).override("resting", int(time.time()) + 60)
                return
            time.sleep(0.05)

    observer = threading.Thread(target=rest_after_child_starts)
    observer.start()
    try:
        command = unit_command(unit, root, 30, ["/usr/bin/python3", "-c", SLEEP_CODE])
        code, events, ended = execute(command, root.parent / "prompt.txt", store, run)
        observer.join(timeout=10)
        assert observed.is_set(), "Observe a real child before changing the schedule"
        assert ended and unit_empty(unit) and not has_sleep(unit)
        assert events.completed == 0
        store.finish_failed(run, "execution_failed")
        assert store.get(run)["status"] == "cancelled"
    finally:
        stop_unit(unit)
        observer.join(timeout=10)
        (root.parent / "credential.env").unlink(missing_ok=True)


def test_recovery_stops_automatic_orphan_and_blocks_a_billed_retry(tmp_path, monkeypatch):
    store, run, root = setup_run(tmp_path, monkeypatch, 60, automatic=True)
    unit = store.get(run)["unit_name"]
    command = unit_command(unit, root, 30, ["/usr/bin/python3", "-c", SLEEP_CODE])
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not has_sleep(unit) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert has_sleep(unit)
        store.started(run)
        assert Worker(store.database).recover() == run
        assert store.active() is None and unit_empty(unit) and not has_sleep(unit)
        assert DispatchStore(store.database).reserve() is None
        with store.database.connect() as connection:
            assert (
                connection.execute(
                    "SELECT blocked_reason FROM wake_event_delivery "
                    "JOIN wake_events ON wake_events.id = wake_event_delivery.event_id "
                    "WHERE wake_events.kind = 'message'"
                ).fetchone()[0]
                == "interrupted"
            )
    finally:
        stop_unit(unit)
        process.wait(timeout=5)
        (root.parent / "credential.env").unlink(missing_ok=True)

"""Real CLI signals and process trees, using a synthetic profile and sleep only."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from test_process_lifecycle import SLEEP_CODE, has_sleep
from test_scheduler_management import private_policy

from milo_home.database import Database
from milo_home.management_lease import current_boot, observe_management
from milo_home.run_transport import stop_unit, unit_empty
from milo_home.runs import RunStore
from milo_home.schedule_models import SchedulePolicy
from milo_home.schedules import ScheduleStore
from milo_home.wakeup_events import EventPlanner

pytestmark = pytest.mark.skipif(
    os.getuid() != 0 or not Path("/run/systemd/system").exists(),
    reason="Requires root/systemd for managed CLI process-tree verification",
)

BOOTSTRAP = """
import sys
from pathlib import Path
import milo_home.worker as worker
from milo_home.run_environment import prepare
policy, profile, sleep_code = sys.argv[1:]
worker.prepare = lambda base, snapshot, ignored: prepare(base, snapshot, Path(profile))
worker.codex_arguments = lambda: ['/usr/bin/python3', '-c', sleep_code]
sys.argv = ['milo-home', 'scheduler', 'run', '--policy', policy,
            '--cycles', '3', '--timeout', '30', '--execute']
from milo_home.cli import main
main()
"""


def environment(database):
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "MILO_DATABASE": str(database.path),
        "MILO_SCHEDULER_SIGNAL_TEST": "synthetic-only",
    }


def wait_until(process, predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and process.poll() is None:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Expected synthetic process state was not observed")


def finish(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=5)


def test_real_sigterm_interrupts_idle_cli_wait(tmp_path):
    database = Database(tmp_path / "home.sqlite3")
    database.initialize()
    policy = private_policy(tmp_path)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "milo_home.cli",
            "scheduler",
            "run",
            "--policy",
            str(policy),
            "--cycles",
            "3",
            "--interval",
            "300",
            "--execute",
        ],
        env=environment(database),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def scanned():
        with database.connect() as connection:
            return (
                connection.execute("SELECT last_scan_at FROM wake_event_cursor").fetchone()[0]
                is not None
            )

    try:
        wait_until(process, scanned)
        process.send_signal(signal.SIGTERM)
        output, error = process.communicate(timeout=8)
        assert process.returncode == 0, error
        report = json.loads(output)
        assert (report["cycles"], report["reason"]) == (1, "stop_requested")
        assert RunStore(database).active() is None
        with database.connect() as connection:
            lease = connection.execute("SELECT * FROM management_lease").fetchone()
        assert observe_management(lease, int(time.time()), current_boot())[0] == "stopped"
    finally:
        finish(process)


def test_real_sigterm_cancels_child_tree_and_removes_credential(tmp_path):
    database = Database(tmp_path / "home.sqlite3")
    database.initialize()
    owner, thread = str(uuid4()), str(uuid4())
    with database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO users VALUES (?, 'signal', '测试', 'synthetic', 0)", (owner,)
        )
        connection.execute("INSERT INTO threads VALUES (?, ?, 0)", (thread, owner))
        connection.execute(
            "INSERT INTO messages (id, thread_id, sequence, role, body, author_id, client_key, "
            "created_at) VALUES (?, ?, 1, 'visitor', '合成信号检查', ?, ?, 0)",
            (str(uuid4()), thread, owner, str(uuid4())),
        )
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
    policy = private_policy(tmp_path)
    profile = tmp_path / "profile.toml"
    profile.write_text("""model = "synthetic"
model_provider = "fixture"
[model_providers.fixture]
name = "fixture"
base_url = "https://example.invalid"
env_key = "MILO_SCHEDULER_SIGNAL_TEST"
wire_api = "responses"
""")
    process = subprocess.Popen(
        [sys.executable, "-c", BOOTSTRAP, str(policy), str(profile), SLEEP_CODE],
        env=environment(database),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    store = RunStore(database)
    observed = []

    def child_running():
        run = store.active()
        if run and has_sleep(run["unit_name"]):
            observed.append(run)
            return True
        return False

    try:
        wait_until(process, child_running)
        process.send_signal(signal.SIGTERM)
        output, error = process.communicate(timeout=12)
        assert process.returncode == 0, error
        report = json.loads(output)
        assert (report["cycles"], report["reason"]) == (1, "run_cancelled")
        run = store.get(observed[0]["id"])
        assert run["status"] == "cancelled" and store.active() is None
        assert unit_empty(run["unit_name"]) and not has_sleep(run["unit_name"])
        assert not (tmp_path / "runs" / run["id"] / "credential.env").exists()
        with database.connect() as connection:
            assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
            assert (
                connection.execute("SELECT count(*) FROM messages WHERE role='milo'").fetchone()[0]
                == 0
            )
            assert connection.execute("SELECT ended_at FROM management_lease").fetchone()[0]
    finally:
        finish(process)
        if observed:
            stop_unit(observed[0]["unit_name"])
            (tmp_path / "runs" / observed[0]["id"] / "credential.env").unlink(missing_ok=True)


def test_sigkill_expires_evidence_and_competing_cli_cannot_claim(tmp_path):
    from test_scheduler_management import command

    database = Database(tmp_path / "home.sqlite3")
    database.initialize()
    policy = private_policy(tmp_path)
    arguments = ["run", "--policy", str(policy), "--cycles", "3", "--interval", "300", "--execute"]
    process = subprocess.Popen(
        [sys.executable, "-m", "milo_home.cli", "scheduler", *arguments],
        env=environment(database),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def claimed():
        with database.connect() as connection:
            return connection.execute("SELECT 1 FROM management_lease").fetchone() is not None

    try:
        wait_until(process, claimed)
        competitor = command(database.path, *arguments)
        assert competitor.returncode != 0
        assert "自动管理仍有有效占用" in competitor.stderr
        check = command(database.path, "check", "--policy", str(policy))
        assert check.returncode == 0
        process.kill()
        process.communicate(timeout=5)
        with database.connect() as connection:
            lease = connection.execute("SELECT * FROM management_lease").fetchone()
            assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert lease["ended_at"] is None
        assert observe_management(lease, lease["heartbeat_at"] + 45, current_boot()) == (
            "unknown",
            "management_lease_expired",
            None,
        )
    finally:
        finish(process)

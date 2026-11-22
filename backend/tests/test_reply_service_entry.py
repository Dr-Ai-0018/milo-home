import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from uuid import uuid4

import pytest
from test_reply_readiness import preparation as preparation
from test_work_service_entry import control, properties, wait_for

from milo_home.automatic_services import AutomaticServices
from milo_home.reply_recovery import recover
from milo_home.runs import RunStore
from milo_home.schedules import ScheduleStore
from milo_home.wakeup_events import EventPlanner
from milo_home.work_access import WorkAccess
from milo_home.work_tasks import WorkTasks

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not Path("/run/systemd/system").exists(), reason="Requires root systemd"
)


@pytest.fixture
def service_case(preparation):
    database, owner, thread, _, directory, profile, key = preparation
    ScheduleStore(database).override("working", int(time.time()) + 3600)
    workspace = str(uuid4())
    access = WorkAccess(database)
    access.register_workspace(workspace, "合成工作桌", directory)
    access.grant(owner)
    session, _ = WorkTasks(database).main_session(owner, workspace)
    stem = "milo-test-reply-" + uuid4().hex
    unit, timer = stem + ".service", stem + ".timer"
    services = AutomaticServices(database)
    services.configure("work", owner, "milo-home-work.timer", session=session["id"])
    services.configure("reply", owner, timer, enabled=True)
    settings, report = directory / "service.json", directory / "report.json"
    settings.write_text(json.dumps({"mode": "normal", "report": str(report)}))
    wrapper = Path(__file__).parent / "fixtures/reply_service_entry.py"
    args = [
        sys.executable,
        str(wrapper),
        str(settings),
        "--run-directory",
        str(directory),
        "reply-service",
        "take-next",
        "--owner",
        owner,
        "--profile",
        str(profile),
        "--credential-file",
        "${CREDENTIALS_DIRECTORY}/provider",
        "--timeout",
        "8",
        "--execute",
        "--model",
        "synthetic-reply-selected-model",
    ]
    command = ["systemd-run", "--quiet", "--unit=" + stem, "--on-active=1h"]
    for value in [
        "Type=exec",
        "Restart=no",
        "RuntimeMaxSec=12",
        "TimeoutStopSec=5",
        "KillMode=mixed",
        "CPUQuota=50%",
        "CPUQuotaPeriodSec=10ms",
        "MemoryMax=768M",
        "TasksMax=64",
        "UMask=0077",
        "AllowedCPUs=" + ",".join(map(str, sorted(os.sched_getaffinity(0)))),
        "LoadCredential=provider:" + str(key),
        "Environment=MILO_DATABASE=" + str(database.path),
        "Environment=PYTHONPATH=" + str(Path(__file__).parents[1] / "src"),
    ]:
        command += ["-p", value]
    installed = subprocess.run(command + ["--", *args], capture_output=True, timeout=8)
    assert installed.returncode == 0, installed.stderr.decode()

    def start(mode="normal", *, wait=True):
        settings.write_text(json.dumps({"mode": mode, "report": str(report)}))
        assert control("start", unit).returncode == 0
        if wait:
            wait_for(lambda: properties(unit).get("ActiveState") in {"inactive", "failed"})
            return json.loads(report.read_text())

    yield preparation, services, start, unit, timer
    control("stop", timer, unit)
    with database.snapshot() as connection:
        units = [row[0] for row in connection.execute("SELECT unit_name FROM runs")]
    for inner in units:
        control("stop", inner)
        control("reset-failed", inner)
    control("reset-failed", unit, timer)


def send(client, service_case, count=1):
    preparation, *_ = service_case
    database, _, thread, *_ = preparation
    for _ in range(count):
        response = client.post(
            f"/api/threads/{thread}/messages",
            json={"body": "合成独立服务留言"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 201
    EventPlanner(database).plan()
    with database.connect(write=True) as connection:
        connection.execute("UPDATE wake_events SET observed_at=observed_at-16 WHERE kind='message'")


def test_real_timer_service_publishes_two_batches_and_cleans_credentials(client, service_case):
    preparation, services, start, unit, _ = service_case
    database, owner, thread, _, directory, *_ = preparation
    send(client, service_case, 12)
    assert start()["outcome"] == "succeeded"
    assert client.get("/api/threads").json()[0]["pending_count"] == 2
    assert start()["outcome"] == "succeeded"
    assert client.get("/api/threads").json()[0]["pending_count"] == 0
    assert start()["outcome"] == "waiting_or_other_entry_first"
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM reply_execution_results").fetchone()[0] == 2
        conversation = connection.execute(
            "SELECT id FROM conversations WHERE thread_id=?", (thread,)
        ).fetchone()[0]
    status = client.get(f"/api/conversations/{conversation}/activity").json()
    assert status["reply_service"]["state"] == "scheduled"
    services.disable("reply", owner)
    assert start()["outcome"] == "service_disabled_or_faulted"
    assert not list(directory.glob("*/credential.env"))
    for path in directory.glob("*/rootfs/runtime/codex/newawz.config.toml"):
        assert tomllib.loads(path.read_text())["model"] == "synthetic-reply-selected-model"
    assert not Path("/run/credentials", unit).exists()
    assert properties(unit)["MainPID"] == "0"


@pytest.mark.parametrize("signal", ["TERM", "KILL"])
def test_real_service_stop_and_crash_do_not_automatically_relaunch(client, service_case, signal):
    preparation, services, start, unit, _ = service_case
    database, owner, _, _, directory, *_ = preparation
    send(client, service_case)
    start("hang", wait=False)
    store = RunStore(database)

    def executing():
        run = store.active()
        return bool(run and (directory / run["id"] / "rootfs/runtime/descendant").exists())

    wait_for(executing)
    run = store.active()
    if signal == "TERM":
        assert control("stop", unit).returncode == 0
        assert store.get(run["id"])["status"] == "cancelled"
        assert not list(directory.glob("*/credential.env"))
    else:
        assert control("kill", "--kill-who=main", "--signal=KILL", unit).returncode == 0
        wait_for(lambda: properties(unit).get("ActiveState") == "failed")
        assert store.get(run["id"])["status"] in {"running", "queued"}
        assert start()["outcome"] == "needs_reconciliation"
        assert (
            recover(database, directory, owner, run["id"])["outcome"]
            == "reconciled_without_relaunch"
        )
    assert start()["outcome"] in {"service_disabled_or_faulted", "waiting_or_other_entry_first"}
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM messages WHERE role='milo'").fetchone()[0] == 0
        )
    assert properties(run["unit_name"] + ".service").get("MainPID", "0") == "0"
    assert not list(directory.glob("*/credential.env"))

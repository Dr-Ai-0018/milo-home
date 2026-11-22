import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_execution_control import execution_case as execution_case
from test_work_cli import runtime_case as runtime_case
from test_work_execution import counts
from test_work_process import launch_case as launch_case
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.app import create_app
from milo_home.auth import digest
from milo_home.backup_process_evidence import observe_recorded
from milo_home.config import Settings
from milo_home.errors import ApiError

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not Path("/run/systemd/system").exists(), reason="Requires root systemd"
)


def control(*args):
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True, timeout=10, check=False
    )


def properties(unit):
    result = control("show", unit, "-p", "ActiveState", "-p", "MainPID", "-p", "NRestarts")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def wait_for(predicate, *, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail("Synthetic service did not reach expected state")


@pytest.fixture
def service_case(launch_case):
    case, runtime, journal, launcher, output, _ = launch_case
    home = journal.path.parent
    source = home / "provider.key"
    source.write_text("synthetic-service-secret\n")
    source.chmod(0o600)
    settings = home / "service-fixture.json"
    unit = "milo-test-receiver-" + uuid4().hex + ".service"
    wrapper = Path(__file__).parent / "fixtures/work_service_entry.py"
    launched = []

    def start(*, mode="normal", seconds=12, descendant=False):
        settings.write_text(
            json.dumps(
                {
                    "content": launcher.config.content.decode(),
                    "now": case[-1][0],
                    "scenario": mode,
                    "output": str(output),
                    "descendant": descendant,
                }
            )
        )
        args = [
            sys.executable,
            str(wrapper),
            str(settings),
            "--run-directory",
            str(home),
            "work",
            "check",
            "--owner",
            case[0][1],
            "--session",
            case[0][-1]["id"],
            "--timeout",
            str(seconds),
            "--credential-file",
            "${CREDENTIALS_DIRECTORY}/newawz-key",
        ]
        command = [
            "systemd-run",
            "--quiet",
            "--wait",
            "--pipe",
            "--unit=" + unit,
            "-p",
            "Type=exec",
            "-p",
            "Restart=no",
            "-p",
            "KillMode=mixed",
            "-p",
            "TimeoutStartSec=10",
            "-p",
            "TimeoutStopSec=8",
            "-p",
            "RuntimeMaxSec=20",
            "-p",
            "CPUQuota=50%",
            "-p",
            "CPUQuotaPeriodSec=10ms",
            "-p",
            "AllowedCPUs=" + ",".join(map(str, sorted(os.sched_getaffinity(0)))),
            "-p",
            "MemoryMax=768M",
            "-p",
            "TasksMax=64",
            "-p",
            "UMask=0077",
            "-p",
            "LoadCredential=newawz-key:" + str(source),
            "-p",
            "Environment=MILO_DATABASE=" + str(runtime.database.path),
            "-p",
            "ExecStartPre=" + shlex.join(args),
        ]
        args[args.index("check")] = "listen"
        command += ["--", *args, "--execute"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        launched.append(process)
        return process

    yield start, unit, source, output
    control("stop", unit)
    for process in launched:
        process.communicate(timeout=12)
    for stream in journal.outstanding():
        control("stop", stream["unit"])
    control("reset-failed", unit)


def test_real_service_loads_credential_runs_one_task_and_does_not_restart(
    launch_case, service_case
):
    start, unit, source, output = service_case
    case = launch_case[0][0]
    first, second = create(case), create(case)
    process = start()
    out, err = process.communicate(timeout=20)
    assert process.returncode == 0, err.decode()
    reports = [json.loads(line) for line in out.splitlines()]
    assert reports[0]["ready"] and reports[-1]["task_id"] == first["id"]
    assert reports[-1]["outcome"] == "completed"
    assert case[5].get(case[1], second["id"])["status"] == "queued"
    assert counts(launch_case[1].database) == [1, 1, 1]
    assert properties(unit)["NRestarts"] == "0" and properties(unit)["MainPID"] == "0"
    assert source.exists() and not list(source.parent.glob("*.env"))
    assert not Path("/run/credentials", unit).exists()
    assert b"synthetic-service-secret" not in out + err + output.read_bytes()


@pytest.mark.parametrize("mode", ["reject", "idle", "failed"])
def test_real_service_rejection_finite_idle_and_failure_do_not_restart(
    launch_case, service_case, mode
):
    start, unit, source, output = service_case
    if mode == "reject":
        source.write_text("invalid\nmultiline")
    elif mode == "failed":
        create(launch_case[0][0])
    process = start(
        mode="failed-terminal" if mode == "failed" else "normal",
        seconds=8 if mode == "failed" else 3,
    )
    out, err = process.communicate(timeout=15)
    assert process.returncode != 0
    assert properties(unit)["NRestarts"] == "0" and properties(unit)["MainPID"] == "0"
    assert not Path("/run/credentials", unit).exists()
    assert not list(source.parent.glob("*.env"))
    if mode != "failed":
        assert counts(launch_case[1].database) == [0, 0, 0] and not output.exists()
    if mode == "idle":
        assert out.splitlines(), err.decode()
        assert json.loads(out.splitlines()[-1])["outcome"] == "wait_expired"
    elif mode == "failed":
        assert out.splitlines(), err.decode()
        assert json.loads(out.splitlines()[-1]).get("outcome") == "failed", err.decode()
    assert b"synthetic-service-secret" not in out + err


@pytest.mark.parametrize("signal", ["TERM", "KILL"])
def test_real_inflight_outer_stop_preserves_exact_family_semantics(
    launch_case, service_case, signal
):
    start, unit, source, output = service_case
    case, runtime, journal, _, _, _ = launch_case
    create(case[0])
    process = start(mode="interrupt", seconds=7, descendant=True)
    wait_for(lambda: output.exists() and '"turn/start"' in output.read_text())
    stream = journal.outstanding()[0]
    inner = stream["unit"]
    child = int(Path(str(output) + ".child").read_text())
    if signal == "TERM":
        assert control("stop", unit).returncode == 0
    else:
        assert control("kill", "--kill-who=main", "--signal=KILL", unit).returncode == 0
    process.communicate(timeout=12)
    if signal == "TERM":
        assert not journal.outstanding()
        assert not list(source.parent.glob("*.env"))
    else:
        assert journal.outstanding()
        with runtime.database.connect() as connection:
            assert (
                connection.execute("SELECT state FROM execution_windows").fetchone()[0] != "closed"
            )
        # The sibling remains independently bounded by its original deadline.
        wait_for(lambda: properties(inner).get("MainPID", "0") == "0", seconds=10)
    assert properties(inner).get("MainPID", "0") == "0"
    stat = Path("/proc", str(child), "stat")
    assert not stat.exists() or stat.read_text().rsplit(") ", 1)[1].startswith("Z ")
    assert properties(unit)["NRestarts"] == "0"


def test_http_stop_during_acknowledged_turn_confirms_family_but_retains_unknown(
    launch_case, service_case
):
    start, _, source, output = service_case
    case, runtime, journal, _, _, _ = launch_case
    database, owner, _, _, _, tasks, _ = case[0]
    task = create(case[0])
    tasks.append(owner, task["id"], "这段没有获准递出", str(uuid4()))
    path = f"/api/work-tasks/{task['id']}"
    origin = "https://home.test"
    token = secrets.token_urlsafe(32)
    now = int(datetime.now(UTC).timestamp())
    with database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)", (digest(token), owner, now, now + 3600)
        )

    with TestClient(
        create_app(Settings(database=database.path, origins=(origin,))), base_url=origin
    ) as browser:
        browser.cookies.set("milo_session", token)
        process = start(mode="interrupt", seconds=15, descendant=True)

        def active():
            response = browser.get(path)
            assert response.status_code == 200, response.text
            detail = response.json()
            return (
                detail["status"] == "running"
                and detail["initial_input"]["delivery"] == "acknowledged"
            )

        wait_for(active, seconds=8)
        stream = journal.outstanding()[0]
        child = int(Path(str(output) + ".child").read_text())
        assert Path("/proc", str(child), "stat").exists()
        response = browser.post(
            path + "/stop", headers={"Origin": origin, "Idempotency-Key": str(uuid4())}, json={}
        )
        assert response.status_code == 202
        assert response.json()["status"] == "stopping"
        process.communicate(timeout=8)

        detail = browser.get(path).json()
        assert detail["status"] == "needs_reconciliation"
        assert detail["stop_requested_at"] is not None
        inputs = browser.get(path + "/inputs").json()["inputs"]
        assert [item["delivery"] for item in inputs] == ["acknowledged", "cancelled"]
        assert browser.get(path + "/results").json() == {"results": []}
        events = browser.get(path + "/events").json()["events"]
        assert [e["state"] for e in events if e["kind"] == "task_status"] == [
            "queued",
            "running",
            "stopping",
            "needs_reconciliation",
        ]

    observation = observe_recorded(journal.path.parent)
    assert observation["recorded_families_absent_observed"]
    assert [f["unit"] for f in observation["families"]] == [stream["unit"]]
    stat = Path("/proc", str(child), "stat")
    assert not stat.exists() or stat.read_text().rsplit(") ", 1)[1].startswith("Z ")
    assert not list(source.parent.glob("*.env"))
    assert counts(runtime.database) == [1, 1, 1]
    assert (
        sum(json.loads(line)["method"] == "turn/start" for line in output.read_text().splitlines())
        == 1
    )
    with pytest.raises(ApiError, match="work_state_changed"):
        case[1].reserve(owner, task["id"], 15)
    next_task = create(case[0], "下一张单还不能领取")
    with pytest.raises(ApiError, match="run_busy"):
        case[1].reserve(owner, next_task["id"], 15)
    assert counts(runtime.database) == [1, 1, 1]


def work_command(runtime, owner, action, *arguments):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "milo_home.cli",
            "--run-directory",
            str(runtime.journal.path.parent),
            "work",
            action,
            "--owner",
            owner,
            *arguments,
        ],
        env={**os.environ, "MILO_DATABASE": str(runtime.database.path)},
        capture_output=True,
        text=True,
        timeout=12,
    )
    return result.returncode, json.loads(result.stdout)


def test_real_recovery_does_not_release_stopped_turn_without_terminal(launch_case, service_case):
    start, unit, source, output = service_case
    case, runtime, journal, _, _, _ = launch_case
    database, owner, _, _, _, tasks, _ = case[0]
    task = create(case[0])
    runner = start(mode="interrupt", seconds=15, descendant=True)
    wait_for(lambda: tasks.get(owner, task["id"])["initial_input"]["delivery"] == "acknowledged")
    stream = journal.outstanding()[0]
    assert work_command(runtime, owner, "stop", "--task", task["id"], "--key", str(uuid4())) == (
        0,
        {"task_status": "stopping", "stop_requested": True},
    )
    runner.communicate(timeout=8)
    assert properties(unit)["MainPID"] == "0"
    assert observe_recorded(journal.path.parent)["recorded_families_absent_observed"]
    assert not list(source.parent.glob("*.env"))

    code, status = work_command(runtime, owner, "status", "--task", task["id"])
    assert code == 0 and status["task_status"] == "needs_reconciliation"
    (window,) = status["windows"]
    assert window["state"] == "unknown"
    args = ("--window", window["id"])
    before = output.read_bytes()
    private = journal.path.read_bytes()
    code, report = work_command(runtime, owner, "inspect", *args)
    assert code == 0 and report["requires_reconciliation"]
    assert report["recorded_stop"] == "stop_confirmed"
    assert report["missing_receipts"] == 1
    assert report["known_turns"] == report["published_turns"] == 0
    assert journal.path.read_bytes() == private
    assert work_command(runtime, owner, "settle", *args, "--confirm-stop") == (
        1,
        {"closed": False, "outcome": "needs_reconciliation", "blocked_by": ["missing_receipts"]},
    )
    assert output.read_bytes() == before and counts(database) == [1, 1, 1]
    assert journal.work_snapshot(stream["id"])["stream"]["id"] == stream["id"]
    assert work_command(runtime, owner, "status", "--task", task["id"]) == (0, status)
    next_task = create(case[0], "下一张单还不能领取")
    with pytest.raises(ApiError, match="run_busy"):
        case[1].reserve(owner, next_task["id"], 15)

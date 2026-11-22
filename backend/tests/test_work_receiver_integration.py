import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_work_cli import runtime_case as runtime_case
from test_work_coordinator import starts
from test_work_execution import counts
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_receiver import invoke, receive, receiver_for
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.app import create_app
from milo_home.auth import digest
from milo_home.codex_rpc import DeliveryUnknown, ProtocolError
from milo_home.config import Settings
from milo_home.database_identity import ServiceDatabase
from milo_home.errors import ApiError
from milo_home.execution_lock import execution_lock
from milo_home.work_progress import WorkProgress
from milo_home.work_runtime import WorkRuntime

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root receiver")


def test_http_saved_task_to_independent_cli_and_public_results(
    launch_case, fake_units, monkeypatch, capsys
):
    import json

    case, runtime, journal, _, output, _ = launch_case
    owner = case[0][1]
    origin = "https://synthetic.test"
    token = "s" * 43
    with runtime.database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            (digest(token), owner, case[-1][0], case[-1][0] + 3600),
        )
    settings = Settings(database=runtime.database.path, origins=(origin,))
    app = create_app(settings)
    with TestClient(app, base_url=origin) as client:
        app.state.auth.clock = runtime.clock
        app.state.work_tasks.clock = runtime.clock
        client.cookies.set("milo_session", token)
        response = client.post(
            f"/api/work-sessions/{case[0][-1]['id']}/tasks",
            headers={"Origin": origin, "Idempotency-Key": str(uuid4())},
            json={"body": "合成网页工作，不操作实际项目"},
        )
        assert response.status_code == 202
        task = response.json()
        assert task["execution_available"] is False
        assert counts(runtime.database) == [0, 0, 0] and not output.exists()
    # The HTTP worker has exited: the root CLI remains strictly single-threaded.
    invoke(monkeypatch, launch_case)
    assert json.loads(capsys.readouterr().out)["task_id"] == task["id"]
    with TestClient(app, base_url=origin) as client:
        app.state.auth.clock = runtime.clock
        client.cookies.set("milo_session", token)
        assert client.get(f"/api/work-tasks/{task['id']}").json()["status"] == "completed"
        result = client.get(f"/api/work-tasks/{task['id']}/results").json()["results"][0]
        assert result["output"]["answer"]["body"] == "合成结果"
        events = client.get(f"/api/work-tasks/{task['id']}/events").json()["events"]
        assert events and all("synthetic-main" not in str(event) for event in events)
    assert len(starts(output)) == 1 and not journal.outstanding()


def test_cancel_race_after_selection_never_reserves_or_spawns(launch_case, fake_units, monkeypatch):
    case, runtime, _, _, output, _ = launch_case
    task = create(case[0])
    receiver = receiver_for(launch_case)
    original = receiver._next_task

    def cancel(*args):
        selected = original(*args)
        case[0][5].stop(case[0][1], task["id"], str(uuid4()))
        return selected

    monkeypatch.setattr(receiver, "_next_task", cancel)
    with pytest.raises(ApiError, match="work_state_changed"):
        receive(launch_case, receiver)
    assert counts(runtime.database) == [0, 0, 0] and not output.exists() and not fake_units[1]


def test_second_receiver_cannot_claim_during_selection(launch_case, fake_units, monkeypatch):
    create(launch_case[0][0])
    first, second = receiver_for(launch_case), receiver_for(launch_case)
    original = first._next_task

    def competing(*args):
        with pytest.raises(ApiError, match="worker_busy"):
            receive(launch_case, second)
        return original(*args)

    monkeypatch.setattr(first, "_next_task", competing)
    result = receive(launch_case, first)
    assert result.outcome == "completed" and counts(launch_case[1].database) == [1, 1, 1]


def test_regrant_between_session_check_and_selection_requires_new_receiver(
    launch_case, fake_units, monkeypatch
):
    case, runtime, _, _, output, _ = launch_case
    receiver = receiver_for(launch_case)
    original = receiver._next_task
    queued = []

    def regrant(*args):
        case[0][4].revoke(case[0][1])
        case[0][4].grant(case[0][1])
        queued.append(create(case[0]))
        return original(*args)

    monkeypatch.setattr(receiver, "_next_task", regrant)
    with pytest.raises(ProtocolError, match="authorization changed"):
        receive(launch_case, receiver)
    assert counts(runtime.database) == [0, 0, 0] and not output.exists() and not fake_units[1]
    assert case[0][5].get(case[0][1], queued[0]["id"])["status"] == "queued"


def test_preparation_and_idle_share_original_deadline(launch_case, fake_units, monkeypatch):
    case, runtime, _, launcher, _, _ = launch_case
    receiver = receiver_for(launch_case, seconds=60)
    original = launcher._prepare

    def arrive(seconds):
        case[-1][0] += 20
        create(case[0])

    def prepare(credential, *, execute):
        result = original(credential, execute=execute)
        case[-1][0] += 10
        return result

    monkeypatch.setattr(receiver.stop, "wait", arrive)
    monkeypatch.setattr(launcher, "_prepare", prepare)
    result = receive(launch_case, receiver)
    assert result.outcome == "completed"
    with runtime.database.connect() as connection:
        window = connection.execute("SELECT * FROM execution_windows").fetchone()
    assert window["deadline_at"] <= receiver.limit.wall_deadline
    assert window["deadline_at"] - window["created_at"] == 30
    assert fake_units[1][0].deadline <= receiver.limit.monotonic_deadline


def test_lost_response_blocks_receiver_from_taking_next_task(launch_case, fake_units):
    case, runtime, journal, _, output, selected = launch_case
    task = create(case[0])
    next_task = create(case[0])
    selected[0] = "eof"
    with pytest.raises(DeliveryUnknown):
        receive(launch_case)
    with pytest.raises(ProtocolError, match="needs reconciliation"):
        receive(launch_case)
    assert len(starts(output)) == 1 and counts(runtime.database) == [1, 1, 1]
    assert case[0][5].get(case[0][1], task["id"])["status"] == "needs_reconciliation"
    assert case[0][5].get(case[0][1], next_task["id"])["status"] == "queued"
    assert not list(journal.path.parent.glob("*.env"))


@pytest.mark.skipif(not os.path.exists("/run/systemd/system"), reason="Requires systemd")
def test_actual_systemd_synthetic_receiver_stops_and_publishes(launch_case):
    case, runtime, journal, _, output, _ = launch_case
    task = create(case[0])
    result = receive(launch_case)
    assert result.outcome == "completed" and result.task_id == task["id"]
    assert len(starts(output)) == 1
    with runtime.database.connect() as connection:
        stream = connection.execute("SELECT stream_id FROM execution_start_intents").fetchone()[0]
    snapshot = journal.work_snapshot(stream)
    ready = [entry for _, entry in snapshot["entries"] if entry.get("phase") == "ready"]
    assert len(ready) == 1 and ready[0]["main_pid"] > 0 and ready[0]["boot_id"]
    assert snapshot["stream"]["state"] == "closed"
    assert WorkProgress(runtime.database).results(case[0][1], task["id"])["results"]
    assert not list(journal.path.parent.glob("*.env"))


def test_execute_flag_required_without_creating_database(tmp_path):
    from test_cli_entrypoints import command

    path = tmp_path / "absent.sqlite3"
    result = command(
        path,
        "work",
        "listen",
        "--owner",
        str(uuid4()),
        "--session",
        str(uuid4()),
        "--timeout",
        "60",
    )
    assert result.returncode == 2 and "--execute" in result.stderr and not path.exists()


def test_service_account_receiver_keeps_private_journal_at_root(
    runtime_case, owned_database, fake_units, monkeypatch, capsys, tmp_path
):
    import json
    import sys
    from pathlib import Path

    from test_work_runtime import SYNTHETIC_CONFIG

    from milo_home.codex_config import CodexRuntime, NewawzConfig
    from milo_home.work_process import WorkLauncher

    case, original_runtime, journal = runtime_case
    task = create(case[0])
    path, account = owned_database
    copied = path.parent / "receiver-seed.sqlite3"
    original_runtime.database.backup(copied)
    os.chown(copied, account.pw_uid, account.pw_gid)
    os.replace(copied, path)
    runtime = WorkRuntime(ServiceDatabase(path, account.pw_name), journal, original_runtime.clock)
    with execution_lock(runtime.database) as lease:
        runtime.bind(case[0][1], case[0][-1]["id"], lease=lease)
    output = tmp_path / "service-requests.ndjson"
    peer = Path(__file__).parent / "fixtures/codex_app_server.py"
    monkeypatch.setattr(
        CodexRuntime,
        "command",
        lambda _, *, workspace: [sys.executable, str(peer), "normal", str(output)],
    )
    monkeypatch.setattr("milo_home.codex_process.time.time", lambda: case[-1][0])
    config = NewawzConfig(
        "synthetic-provider", "synthetic-model", "SYNTHETIC_KEY", SYNTHETIC_CONFIG
    )
    launcher = WorkLauncher(runtime, config, journal.installation())
    assembled = (case, runtime, journal, launcher, output, ["normal"])
    invoke(monkeypatch, assembled, account=account.pw_name)
    assert json.loads(capsys.readouterr().out)["task_id"] == task["id"]
    assert all(entry.stat().st_uid == account.pw_uid for entry in path.parent.iterdir())
    assert all(entry.stat().st_uid == 0 for entry in journal.path.parent.iterdir())
    assert os.geteuid() == 0

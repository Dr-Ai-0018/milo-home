"""Real intake and launch boundaries; reception never overrides authority or active execution."""

import json
import sys
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_dispatch import make_case
from test_execution_control import execution_case as execution_case
from test_reception import request
from test_work_api import http_case as http_case
from test_work_api import initialize
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.activity import ActivityService
from milo_home.cli import main
from milo_home.errors import ApiError
from milo_home.execution_permits import ExecutionPermits
from milo_home.reception import current, save
from milo_home.wakeup_events import EventPlanner
from milo_home.work_access import require_access
from milo_home.work_receiver import receive_context


def change(database, owner, value, now):
    with database.connect(write=True) as connection:
        return save(connection, owner, value, now)


def test_reply_receives_while_resting_and_only_genuine_input_recovers(client, monkeypatch):
    thread, runs, dispatch, now = make_case(client, 1)
    monkeypatch.setattr("milo_home.messages.time", SimpleNamespace(time=runs.clock))
    owner = client.get("/api/auth/me").json()["id"]
    endpoint = f"/api/threads/{thread}/messages"
    key = str(uuid4())
    sent = client.post(endpoint, headers={"Idempotency-Key": key}, json={"body": "先保存"})
    revision = change(runs.database, owner, request("reply", resume_on="new_input"), now[0])
    assert dispatch.reserve() is None
    with pytest.raises(ApiError, match="reception_closed"):
        runs.queue(thread, 60)
    assert (
        client.post(endpoint, headers={"Idempotency-Key": key}, json={"body": "先保存"}).json()
        == sent.json()
    )
    conversation = client.get(f"/api/threads/{thread}/conversations").json()[0]["id"]
    knocked = client.post(
        f"/api/conversations/{conversation}/knocks",
        json={},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert knocked.status_code == 201
    assert dispatch.reserve() is None
    with runs.database.snapshot() as connection:
        assert current(connection, owner, "reply")["revision"] == revision
        assert connection.execute("SELECT count(*) FROM legacy_launches").fetchone()[0] == 0
    assert (
        client.post(
            endpoint, headers={"Idempotency-Key": str(uuid4())}, json={"body": "后来新增"}
        ).status_code
        == 201
    )
    with runs.database.snapshot() as connection:
        assert current(connection, owner, "reply")["mode"] == "available"
    EventPlanner(runs.database, runs.clock).plan()
    now[0] += 15
    assert dispatch.reserve() is not None


@pytest.mark.parametrize("prior_attempts", [1, 3])
def test_change_after_reservation_blocks_launch_without_poisoning_delivery(client, prior_attempts):
    thread, runs, dispatch, now = make_case(client, 1)
    owner = client.get("/api/auth/me").json()["id"]
    ticket = dispatch.reserve()
    with runs.database.connect(write=True) as connection:
        connection.execute("UPDATE wake_event_delivery SET attempts=?", (prior_attempts,))
    change(runs.database, owner, request("reply"), now[0])
    assert not dispatch.authorize_launch(ticket)
    runs.finish_failed(ticket.run_id, "interrupted")
    assert dispatch.reconcile()["blocked_messages"] == 0
    with runs.database.snapshot() as connection:
        row = connection.execute("SELECT * FROM wake_event_delivery").fetchone()
        assert row["blocked_reason"] is None and row["attempts"] == prior_attempts
        assert (
            connection.execute("SELECT count(*) FROM reception_returned_attempts").fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT authorized_at FROM legacy_launches").fetchone()[0] is None
    change(runs.database, owner, request("reply", "available", None), now[0])
    # Repeated intentional rest consumes no retry allowance, including at the old cap.
    for _ in range(4):
        ticket = dispatch.reserve()
        assert ticket is not None
        change(runs.database, owner, request("reply"), now[0])
        assert not dispatch.authorize_launch(ticket)
        runs.finish_failed(ticket.run_id, "interrupted")
        assert dispatch.reconcile()["blocked_messages"] == 0
        change(runs.database, owner, request("reply", "available", None), now[0])
    ticket = dispatch.reserve()
    assert ticket is not None
    with runs.database.snapshot() as connection:
        assert (
            connection.execute("SELECT attempts FROM wake_event_delivery").fetchone()[0]
            == prior_attempts
        )
        assert connection.execute("SELECT count(*) FROM wake_dispatches").fetchone()[0] == 6


def test_active_reply_is_not_stopped_and_observation_is_private_and_expires(client):
    thread, runs, dispatch, now = make_case(client, 1)
    owner = client.get("/api/auth/me").json()["id"]
    ticket = dispatch.reserve()
    assert dispatch.authorize_launch(ticket) and runs.authorize_launch(ticket.run_id)
    assert runs.started(ticket.run_id)
    change(runs.database, owner, request("reply", "focusing", "at_time", now[0] + 20), now[0])
    assert runs.checkpoint(ticket.run_id)["stop_reason"] is None
    conversation = runs.get(ticket.run_id)["conversation_id"]
    view = ActivityService(runs.database, runs.clock).view(owner, conversation)
    assert view.processing == "running"
    assert view.reception.mode == "focusing" and view.reception.valid_until <= view.valid_until
    assert view.valid_until == now[0] + 20
    assert "reception" not in client.get("/api/schedule").json()
    now[0] += 20
    assert ActivityService(runs.database, runs.clock).view(owner, conversation).reception is None
    with pytest.raises(ApiError):
        ActivityService(runs.database, runs.clock).view(str(uuid4()), conversation)


def test_work_retries_do_not_resume_but_new_supplement_does(execution_case):
    work, control, schedule, now = execution_case
    database, owner, _, _, access, tasks, session = work
    tasks.clock = control.clock
    key = str(uuid4())
    task = create(work, key=key)
    revision = change(database, owner, request("all", resume_on="new_input"), now[0])
    assert not tasks.create(owner, session["id"], "合成任务", key)[1]
    assert tasks.session(owner, session["id"])["reception"].revision == revision
    with pytest.raises(ApiError, match="reception_closed"):
        control.reserve(owner, task["id"], 60)
    with database.snapshot() as connection:
        # Receiver uses the actual authorization generation, not a UI assertion.
        access_row = require_access(connection, owner, session["workspace_id"])
        original = (None, access_row["owner_revision"], access_row["workspace_revision"])
        with pytest.raises(ApiError, match="reception_closed"):
            receive_context(connection, owner, session["id"], original, now[0], 60)
    supplement_key = str(uuid4())
    tasks.append(owner, task["id"], "后来补充", supplement_key)
    with database.snapshot() as connection:
        assert current(connection, owner, "work")["mode"] == "available"
        assert current(connection, owner, "reply")["revision"] == revision
    # A new rest followed by a retry stays resting, even if that retry returns success.
    change(database, owner, request("work", resume_on="new_input"), now[0])
    tasks.append(owner, task["id"], "后来补充", supplement_key)
    with pytest.raises(ApiError, match="reception_closed"):
        control.reserve(owner, task["id"], 60)
    change(database, owner, request("work", "available", None), now[0])
    schedule.pause(True)
    with pytest.raises(ApiError, match="work_schedule_closed"):
        control.reserve(owner, task["id"], 60)
    schedule.pause(False)
    access.revoke(owner)
    with pytest.raises(ApiError, match="work_not_found"):
        control.reserve(owner, task["id"], 60)


def test_already_accepted_work_window_and_unknown_occupancy_remain(execution_case):
    work, control, _, now = execution_case
    database, owner, _, _, _, tasks, _ = work
    tasks.clock = control.clock
    task = create(work)
    window = control.reserve(owner, task["id"], 60)
    permits = ExecutionPermits(database, control.clock)
    permit = permits.reserve(owner, window["id"], task["initial_input"]["id"])
    change(database, owner, request("work"), now[0])
    assert permits.consume(owner, permit["id"], task["initial_input"]["id"], claim_input=True)
    change(database, owner, request("all"), now[0])
    assert control.checkpoint(window["id"])["state"] == "running"
    assert not control.close(
        window["id"], stop=lambda: False, delivery_known=False, outcome="failed"
    )
    change(database, owner, request("all", "available", None), now[0])
    with pytest.raises(ApiError, match="run_busy"):
        control.reserve(owner, create(work)["id"], 60)


def test_work_session_http_observes_only_authorized_category(http_case):
    client, owner, _, workspace, tokens = http_case
    session = initialize(http_case)
    now = int(time.time())
    change(client.app.state.database, owner, request("reply"), now)
    assert client.get(f"/api/work-sessions/{session['id']}").json()["reception"] is None
    change(client.app.state.database, owner, request("work", "later", "at_time", now + 60), now)
    response = client.get(f"/api/work-sessions/{session['id']}")
    assert response.json()["reception"]["mode"] == "later"
    assert response.headers["cache-control"] == "no-store"
    client.cookies.set("milo_session", tokens[1])
    assert client.get(f"/api/work-sessions/{session['id']}").status_code == 404


def test_actual_cli_revises_and_reads_without_migration_or_execution(client, monkeypatch, capsys):
    _, runs, _, now = make_case(client, 1)
    owner = client.get("/api/auth/me").json()["id"]
    monkeypatch.setenv("MILO_DATABASE", str(runs.database.path))
    # The bound default clock is explicit for deterministic CLI tests.
    from milo_home.reception_cli import handle

    monkeypatch.setattr(
        "milo_home.reception_cli.handle", lambda db, args: handle(db, args, clock=runs.clock)
    )
    base = ["milo-home", "reception"]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            *base,
            "set",
            "--owner",
            owner,
            "--scope",
            "all",
            "--mode",
            "later",
            "--resume-on",
            "at_time",
            "--until",
            datetime.fromtimestamp(now[0] + 60, UTC).isoformat(),
        ],
    )
    main()
    saved = json.loads(capsys.readouterr().out)
    assert saved["reply"]["revision"] == saved["work"]["revision"]
    monkeypatch.setattr(sys, "argv", [*base, "show", "--owner", owner])
    main()
    assert json.loads(capsys.readouterr().out) == saved
    with runs.database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0

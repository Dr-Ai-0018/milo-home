from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from conftest import activate, own_thread
from test_conversations import send
from test_runs import pending, result

from milo_home.execution_pulse import identity_hash


def case(client):
    thread, store = pending(client, 1)
    now = [int(store.clock())]
    store.clock = lambda: now[0]
    client.app.state.activity.clock = store.clock
    conversation = client.get(f"/api/threads/{thread}/conversations").json()[0]["id"]
    endpoint = f"/api/conversations/{conversation}/activity"
    return thread, store, now, endpoint


def test_activity_tracks_queue_pulse_expiry_and_actual_reply(client):
    thread, store, now, endpoint = case(client)
    initial = client.get(endpoint).json()
    with store.database.snapshot() as connection:
        oldest = connection.execute("SELECT min(created_at) FROM messages").fetchone()[0]
    assert client.get(endpoint).headers["cache-control"] == "no-store"
    assert initial == {
        "observed_at": now[0],
        "valid_until": now[0] + 45,
        "response_phase": "unknown",
        "processing": "idle",
        "heartbeat_at": None,
        "latest_reply_at": None,
        "reply_queue": {
            "pending_count": 1,
            "available_count": 1,
            "reserved_count": 0,
            "retry_wait_count": 0,
            "blocked_count": 0,
            "oldest_pending_at": oldest,
            "retry_after": None,
            "clock_invalid": False,
            "reason": "schedule_unconfigured",
        },
        "automatic_processing": "unknown",
        "reply_service": {
            "state": "unknown",
            "reason": "observation_missing",
            "observed_at": None,
            "valid_until": None,
        },
        "reply_summary": {
            "unprocessed_count": 1,
            "replied_count": 0,
            "no_reply_count": 0,
            "deferred_count": 0,
            "unknown_count": 0,
        },
        "reception": None,
        "revisits": {
            "pending_count": 0,
            "ready_count": 0,
            "active_count": 0,
            "blocked_count": 0,
            "reconciliation_count": 0,
            "next_due_at": None,
        },
        "automatic_reason": "management_not_configured",
    }
    run = store.queue(thread, 180)
    assert client.get(endpoint).json()["processing"] == "queued"
    identity = store.started(run)
    running = client.get(endpoint).json()
    assert running["processing"] == "running" and running["heartbeat_at"] == now[0]
    assert identity not in str(running) and identity_hash(identity) not in str(running)
    now[0] += 44
    assert client.get(endpoint).json()["processing"] == "running"
    now[0] += 1
    assert client.get(endpoint).json()["processing"] == "unknown"
    assert store.active()["id"] == run
    assert store.heartbeat(run, identity)
    assert client.get(endpoint).json()["valid_until"] == now[0] + 45
    now[0] += 1
    assert store.publish(run, result(store, run))
    ended = client.get(endpoint).json()
    assert ended["processing"] == "idle" and ended["heartbeat_at"] is None
    assert ended["latest_reply_at"] == now[0]
    assert not store.heartbeat(run, identity)


def test_activity_is_specific_to_conversation_and_owner(client):
    thread, store, _, endpoint = case(client)
    chat = client.put(f"/api/threads/{thread}/chat").json()["id"]
    send(client, chat, "另一个会话")
    run = store.queue(thread, 180)
    store.started(run)
    assert client.get(endpoint).json()["processing"] == "running"
    assert client.get(f"/api/conversations/{chat}/activity").json()["processing"] == "idle"
    activate(client, "another")
    hidden = client.get(endpoint)
    missing = client.get(f"/api/conversations/{uuid4()}/activity")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    client.post("/api/auth/logout")
    assert client.get(endpoint).status_code == 401


def test_pulse_identity_cannot_be_reclaimed_or_renew_after_stop(client):
    thread, store, now, endpoint = case(client)
    run = store.queue(thread, 180)
    identity = store.started(run)
    assert store.started(run) is None
    now[0] += 15
    assert not store.heartbeat(run, "wrong-identity")
    assert client.get(endpoint).json()["heartbeat_at"] == now[0] - 15
    assert store.heartbeat(run, identity)
    store.stop(run)
    assert not store.heartbeat(run, identity)
    view = client.get(endpoint).json()
    assert view["processing"] == "stopping" and view["heartbeat_at"] is None
    assert store.active()["id"] == run


def test_clock_rewind_latches_unknown_without_releasing_run(client):
    thread, store, now, endpoint = case(client)
    run = store.queue(thread, 180)
    identity = store.started(run)
    now[0] += 15
    assert store.heartbeat(run, identity)
    now[0] -= 1
    assert client.get(endpoint).json()["processing"] == "unknown"
    assert not store.heartbeat(run, identity)
    now[0] += 2
    assert not store.heartbeat(run, identity)
    assert client.get(endpoint).json()["processing"] == "unknown"
    assert store.active()["id"] == run


@pytest.mark.parametrize("corruption", ["missing", "unit", "started", "future", "deadline"])
def test_inconsistent_or_missing_execution_evidence_is_unknown(client, corruption):
    thread, store, now, endpoint = case(client)
    run = store.queue(thread, 180)
    store.started(run)
    with store.database.connect(write=True) as connection:
        if corruption == "missing":
            connection.execute("DELETE FROM run_activity")
        elif corruption == "unit":
            connection.execute("UPDATE runs SET unit_name = 'wrong-unit'")
        elif corruption == "started":
            connection.execute("UPDATE runs SET started_at = started_at + 1")
        elif corruption == "future":
            connection.execute("UPDATE run_activity SET heartbeat_at = heartbeat_at + 1")
        else:
            now[0] += 180
    view = client.get(endpoint).json()
    assert view["processing"] == "unknown" and view["heartbeat_at"] is None


def test_schedule_boundary_pause_and_clock_anomaly(client):
    from test_schedules import configured, timestamp

    activate(client)
    thread = own_thread(client)
    now = [timestamp("21:29") + 50]
    client.app.state.activity.clock = lambda: now[0]
    schedules = configured(client, "21:29")
    schedules.clock = lambda: now[0]
    endpoint = f"/api/conversations/{thread}/activity"
    view = client.get(endpoint).json()
    assert view["response_phase"] == "working"
    assert view["valid_until"] == now[0] + 10
    now[0] += 10
    assert client.get(endpoint).json()["response_phase"] == "winding_down"
    schedules.pause(True)
    assert client.get(endpoint).json()["response_phase"] == "paused"
    now[0] -= 1
    assert client.get(endpoint).json()["response_phase"] == "unknown"


def test_queued_deadline_expires_without_releasing_occupancy(client):
    thread, store, now, endpoint = case(client)
    run = store.queue(thread, 10)
    assert client.get(endpoint).json()["valid_until"] == now[0] + 10
    now[0] += 10
    assert client.get(endpoint).json()["processing"] == "unknown"
    assert store.active()["id"] == run


def test_pulse_does_not_change_automatic_launch_lease_or_budget_history(client):
    from test_dispatch import make_case

    _, store, dispatch, now = make_case(client)
    ticket = dispatch.reserve(180)
    assert dispatch.authorize_launch(ticket)
    identity = store.started(ticket.run_id)
    with store.database.connect() as connection:
        before = dict(connection.execute("SELECT * FROM wake_dispatches").fetchone())
    now[0] += 15
    assert store.heartbeat(ticket.run_id, identity)
    with store.database.connect() as connection:
        after = dict(connection.execute("SELECT * FROM wake_dispatches").fetchone())
    assert before == after


def test_pulse_cannot_bypass_missing_automatic_launch_permission(client):
    from test_dispatch import make_case

    _, store, dispatch, _ = make_case(client)
    ticket = dispatch.reserve(180)
    assert store.started(ticket.run_id) is None
    assert not dispatch.authorize_launch(ticket)
    assert store.get(ticket.run_id)["status"] == "stopping"
    with store.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM run_activity").fetchone()[0] == 0


def test_concurrent_start_has_one_identity_and_old_identity_cannot_renew_next_run(client):
    thread, store, now, endpoint = case(client)
    run = store.queue(thread, 180)
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(lambda _: store.started(run), range(4)))
    identity = next(value for value in identities if value)
    assert sum(value is not None for value in identities) == 1
    store.stop(run)
    store.finish_failed(run, "cancelled")
    second = store.queue(thread, 180)
    current = store.started(second)
    assert current != identity
    now[0] += 15
    assert not store.heartbeat(second, identity)
    assert client.get(endpoint).json()["heartbeat_at"] == now[0] - 15
    assert store.heartbeat(second, current)

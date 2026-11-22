import json
from uuid import uuid4

import pytest
from conftest import activate, own_thread
from test_conversations import send
from test_dispatch import make_case

from milo_home.reply_scope import ReplyScope, queue_counts
from milo_home.schedules import ScheduleStore


@pytest.fixture
def queued(client):
    thread, runs, dispatch, now = make_case(client, count=4)
    with runs.database.connect(write=True) as connection:
        connection.execute("UPDATE messages SET created_at=?", (now[0] - 10,))
    client.app.state.activity.clock = runs.clock
    return thread, runs, dispatch, now, f"/api/conversations/{thread}/activity"


def test_unanswered_is_not_running_and_read_does_not_discover_or_claim(queued, client):
    _, runs, _, _, endpoint = queued
    with runs.database.snapshot() as connection:
        before = list(connection.iterdump())
    response = client.get(endpoint)
    assert response.status_code == 200
    view = response.json()
    assert view["processing"] == "idle"
    assert view["reply_queue"]["reason"] == "automatic_unknown"
    assert view["reply_queue"]["pending_count"] == 4
    assert view["reply_queue"]["available_count"] == 4
    with runs.database.snapshot() as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        for row in connection.execute("SELECT body FROM messages"):
            assert row[0] not in response.text


def test_mixed_blocked_backoff_and_new_messages_stay_in_original_scope(queued, client):
    thread, runs, dispatch, now, endpoint = queued
    ticket = dispatch.reserve(60)
    runs.finish_failed(ticket.run_id, "configuration_error")
    dispatch.reconcile()
    with runs.database.connect(write=True) as connection:
        events = [row[0] for row in connection.execute("SELECT event_id FROM wake_event_delivery")]
        connection.execute(
            "UPDATE wake_event_delivery SET blocked_reason=NULL,retry_after=? WHERE event_id=?",
            (now[0] + 7, events[0]),
        )
        connection.execute(
            "UPDATE wake_event_delivery SET blocked_reason=NULL WHERE event_id=?", (events[1],)
        )
    send(client, thread, "unscanned-body-must-not-be-echoed")
    with runs.database.connect(write=True) as connection:
        connection.execute("UPDATE messages SET created_at=?", (now[0],))
    response = client.get(endpoint)
    queue = response.json()["reply_queue"]
    assert [queue[k] for k in ["pending_count", "available_count", "blocked_count"]] == [5, 2, 2]
    assert queue["retry_wait_count"] == 1 and queue["reserved_count"] == 0
    assert queue["reason"] == "automatic_unknown"
    assert response.json()["valid_until"] == now[0] + 7
    assert ticket.token not in response.text
    assert "configuration_error" not in response.text
    assert "unscanned-body-must-not-be-echoed" not in response.text
    chat = client.put(f"/api/threads/{thread}/chat").json()["id"]
    send(client, chat, "a-different-conversation")
    with runs.database.connect(write=True) as connection:
        connection.execute("UPDATE messages SET created_at=?", (now[0],))
    assert client.get(endpoint).json()["reply_queue"]["pending_count"] == 5
    assert (
        client.get(f"/api/conversations/{chat}/activity").json()["reply_queue"]["pending_count"]
        == 1
    )
    with runs.database.snapshot() as connection:
        owner = connection.execute("SELECT owner_id FROM threads WHERE id=?", (thread,)).fetchone()[
            0
        ]
        assert (
            queue_counts(connection, ReplyScope(owner).thread_id(connection), now[0]).pending_count
            == 6
        )


@pytest.mark.parametrize("phase", ["resting", "winding_down", "paused"])
def test_schedule_reason_has_real_evidence(queued, client, phase):
    _, runs, _, now, endpoint = queued
    schedules = ScheduleStore(runs.database, runs.clock)
    if phase == "paused":
        schedules.pause(True)
    else:
        schedules.override(phase, now[0] + 60)
    assert client.get(endpoint).json()["reply_queue"]["reason"] == phase


def test_expired_dispatch_keeps_occupancy_and_does_not_consume_or_retry(queued, client):
    _, runs, dispatch, now, endpoint = queued
    ticket = dispatch.reserve(10)
    assert client.get(endpoint).json()["reply_queue"]["reserved_count"] == 4
    assert client.get(endpoint).json()["reply_queue"]["reason"] == "processing"
    now[0] += 11
    assert client.get(endpoint).json()["reply_queue"]["reason"] == "needs_reconciliation"
    assert runs.active()["id"] == ticket.run_id
    with runs.database.snapshot() as connection:
        assert (
            connection.execute("SELECT max(attempts) FROM wake_event_delivery").fetchone()[0] == 1
        )


def test_failed_terminal_requires_reconcile_then_reports_blocked(queued, client):
    _, runs, dispatch, _, endpoint = queued
    ticket = dispatch.reserve(60)
    runs.finish_failed(ticket.run_id, "configuration_error")
    assert client.get(endpoint).json()["reply_queue"]["reason"] == "needs_reconciliation"
    dispatch.reconcile()
    queue = client.get(endpoint).json()["reply_queue"]
    assert queue["reason"] == "blocked" and queue["blocked_count"] == 4


def test_queue_is_not_exposed_to_another_account_or_anonymous(queued, client):
    *_, endpoint = queued
    activate(client, "another")
    own_thread(client)
    missing = client.get(f"/api/conversations/{uuid4()}/activity")
    denied = client.get(endpoint)
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json()
    client.post("/api/auth/logout")
    assert client.get(endpoint).status_code == 401


def test_future_message_is_visible_as_unknown_without_becoming_empty(queued, client):
    _, runs, _, now, endpoint = queued
    with runs.database.connect(write=True) as connection:
        connection.execute("UPDATE messages SET created_at=?", (now[0] + 1,))
    queue = client.get(endpoint).json()["reply_queue"]
    assert queue["pending_count"] == 4 and queue["clock_invalid"]
    assert queue["reason"] == "clock_invalid"
    assert "body" not in json.dumps(queue)


def test_legacy_manager_does_not_claim_recurring_service_is_enabled(queued, client):
    from milo_home.automatic_budget import AutomaticPolicy
    from milo_home.management_lease import ManagementLease

    _, runs, _, _, endpoint = queued
    lease = ManagementLease(
        runs.database,
        AutomaticPolicy(daily_launches=200, daily_seconds=None),
        clock=runs.clock,
    )
    lease.claim()
    view = client.get(endpoint).json()
    assert view["automatic_processing"] == "enabled"
    assert view["reply_service"]["state"] == "unknown"
    assert view["reply_service"]["reason"] == "observation_missing"
    lease.close()

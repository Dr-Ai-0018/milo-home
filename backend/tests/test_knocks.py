import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from conftest import ORIGIN, activate, own_thread
from fastapi.testclient import TestClient
from test_dispatch import make_case
from test_schedules import configured, timestamp

from milo_home.app import create_app


def setup(client):
    user, _ = activate(client)
    thread = own_thread(client)
    service = client.app.state.knocks
    now = [timestamp("10:00")]
    service.clock = lambda: now[0]
    return user.json()["id"], thread, service, now


def knock(client, conversation, key=None, **kwargs):
    return client.post(
        f"/api/conversations/{conversation}/knocks",
        json={},
        headers={"Idempotency-Key": key or str(uuid4())},
        **kwargs,
    )


def test_receipt_retry_is_original_result_and_survives_reopening(client):
    _, thread, service, now = setup(client)
    key = str(uuid4())
    first = knock(client, thread, key)
    assert first.status_code == 201
    receipt = first.json()
    assert receipt["disposition"] == "accepted"
    assert receipt["response_phase"] == "unknown"
    assert receipt["next_allowed_at"] == now[0] + 30
    assert set(receipt) == {
        "id",
        "conversation_id",
        "disposition",
        "received_at",
        "next_allowed_at",
        "response_phase",
    }
    assert first.headers["cache-control"] == "no-store"
    now[0] += 100
    configured(client)
    repeat = knock(client, thread, key)
    assert repeat.status_code == 200 and repeat.json() == receipt
    with TestClient(create_app(client.app.state.settings), base_url=ORIGIN) as reopened:
        reopened.cookies.update(client.cookies)
        reopened.headers["Origin"] = ORIGIN
        assert knock(reopened, thread, key).json() == receipt
    with service.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM knocks").fetchone()[0] == 1


def test_account_window_covers_other_conversations_and_does_not_slide(client):
    _, thread, _, now = setup(client)
    chat = client.put(f"/api/threads/{thread}/chat").json()["id"]
    first = knock(client, thread).json()
    now[0] += 29
    key = str(uuid4())
    merged = knock(client, chat, key).json()
    assert merged["disposition"] == "coalesced"
    assert merged["conversation_id"] == chat
    assert merged["next_allowed_at"] == first["next_allowed_at"]
    now[0] += 1
    next_ = knock(client, chat).json()
    assert next_["disposition"] == "accepted"
    assert next_["next_allowed_at"] == now[0] + 30
    replay = knock(client, chat, key)
    assert replay.status_code == 200 and replay.json() == merged


def test_clock_rewind_coalesces_until_original_boundary(client):
    _, thread, _, now = setup(client)
    first = knock(client, thread).json()
    now[0] -= 3600
    assert knock(client, thread).json()["next_allowed_at"] == first["next_allowed_at"]
    now[0] = first["next_allowed_at"] - 1
    assert knock(client, thread).json()["disposition"] == "coalesced"
    now[0] += 1
    assert knock(client, thread).json()["disposition"] == "accepted"


def test_idempotency_is_account_scoped_and_destination_conflicts(client):
    _, thread, _, _ = setup(client)
    chat = client.put(f"/api/threads/{thread}/chat").json()["id"]
    key = str(uuid4())
    assert knock(client, thread, key).status_code == 201
    assert knock(client, chat, key).status_code == 409
    activate(client, "another")
    unknown = str(uuid4())
    hidden = knock(client, thread, key)
    missing = knock(client, unknown, key)
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    assert knock(client, own_thread(client), key).json()["disposition"] == "accepted"


def test_concurrent_retries_and_new_keys_share_one_signal(client):
    owner, thread, service, now = setup(client)
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        retries = list(pool.map(lambda _: service.receive(owner, thread, key), range(4)))
    assert sum(created for _, created in retries) == 1
    assert len({receipt["id"] for receipt, _ in retries}) == 1
    now[0] += 30
    with ThreadPoolExecutor(max_workers=4) as pool:
        fresh = list(pool.map(lambda _: service.receive(owner, thread, str(uuid4())), range(4)))
    assert sum(receipt["disposition"] == "accepted" for receipt, _ in fresh) == 1
    assert sum(receipt["disposition"] == "coalesced" for receipt, _ in fresh) == 3
    assert len({receipt["next_allowed_at"] for receipt, _ in fresh}) == 1
    with service.database.connect() as connection:
        assert connection.execute("SELECT count(DISTINCT signal_id) FROM knocks").fetchone()[0] == 2


@pytest.mark.parametrize(
    "condition,expected",
    [
        ("working", "working"),
        ("winding_down", "winding_down"),
        ("resting", "resting"),
        ("paused", "paused"),
        ("future", "unknown"),
        ("invalid", "unknown"),
    ],
)
def test_response_condition_is_saved_without_messages_or_execution(client, condition, expected):
    _, thread, service, now = setup(client)
    schedule = configured(client, "10:00")
    schedule.clock = lambda: now[0]
    if condition in {"winding_down", "resting"}:
        now[0] = timestamp("21:30" if condition == "winding_down" else "23:00")
    elif condition == "paused":
        schedule.pause(True)
    elif condition == "future":
        now[0] -= 1
    elif condition == "invalid":
        with service.database.connect(write=True) as connection:
            connection.execute("UPDATE schedule_state SET policy = 'invalid-json'")
    first = knock(client, thread).json()
    assert first["response_phase"] == expected
    assert knock(client, thread).json()["disposition"] == "coalesced"
    with service.database.connect() as connection:
        for table in ["messages", "runs", "wake_events", "wake_dispatches", "run_activity"]:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_knocking_does_not_change_blocked_attempts_active_run_or_candidate(client):
    thread, runs, dispatch, now = make_case(client)
    client.app.state.knocks.clock = lambda: now[0]
    ticket = dispatch.reserve(60)
    assert dispatch.authorize_launch(ticket)
    identity = runs.started(ticket.run_id)
    runs.stop(ticket.run_id)
    tables = [
        "messages",
        "runs",
        "wake_events",
        "wake_dispatches",
        "wake_event_delivery",
        "wake_run_events",
        "run_activity",
        "wake_event_cursor",
    ]
    with runs.database.connect() as connection:
        before = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
    assert knock(client, thread).status_code == 201
    assert not runs.heartbeat(ticket.run_id, identity)
    with runs.database.connect() as connection:
        after = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
    assert before == after
    runs.finish_failed(ticket.run_id, "cancelled")
    dispatch.reconcile()
    with runs.database.connect() as connection:
        blocked = [dict(row) for row in connection.execute("SELECT * FROM wake_event_delivery")]
    assert blocked and all(row["blocked_reason"] is not None for row in blocked)
    now[0] += 30
    assert knock(client, thread).json()["disposition"] == "accepted"
    assert dispatch.reserve(60) is None
    with runs.database.connect() as connection:
        assert [
            dict(row) for row in connection.execute("SELECT * FROM wake_event_delivery")
        ] == blocked


def test_http_requires_auth_origin_uuid_and_empty_object(client):
    _, thread, _, _ = setup(client)
    endpoint = f"/api/conversations/{thread}/knocks"
    for body in [{"body": "执行点什么"}, {"phase": "working"}, [], None]:
        response = client.post(endpoint, json=body, headers={"Idempotency-Key": str(uuid4())})
        assert response.status_code == 422
    assert client.post(endpoint, json={}).status_code == 422
    assert client.post(endpoint, json={}, headers={"Idempotency-Key": "bad"}).status_code == 422
    assert (
        client.post(
            endpoint,
            json={},
            headers={"Origin": "https://other.test", "Idempotency-Key": str(uuid4())},
        ).status_code
        == 403
    )
    client.post("/api/auth/logout")
    assert knock(client, thread).status_code == 401


def test_database_rejects_cross_owner_scope_and_invalid_signal(client):
    owner, thread, service, _ = setup(client)
    receipt = knock(client, thread).json()
    another, _ = activate(client, "another")
    other_thread = own_thread(client)
    with service.database.connect(write=True) as connection:
        for params in [
            (str(uuid4()), another.json()["id"], thread, thread, str(uuid4()), "accepted"),
            (str(uuid4()), owner, thread, other_thread, str(uuid4()), "accepted"),
            (str(uuid4()), owner, thread, thread, str(uuid4()), "coalesced"),
        ]:
            id_, owner_, thread_, conversation_, signal, disposition = params
            if disposition == "accepted":
                signal = id_
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO knocks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown')",
                    (
                        id_,
                        owner_,
                        thread_,
                        conversation_,
                        str(uuid4()),
                        signal,
                        disposition,
                        receipt["received_at"],
                        receipt["next_allowed_at"],
                    ),
                )

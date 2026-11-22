import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from conftest import activate, own_thread

from milo_home.errors import ApiError
from milo_home.reply_result import ReplyResult
from milo_home.runs import RunStore


def pending(client, count=2):
    activate(client)
    thread = own_thread(client)
    for index in range(count):
        response = client.post(
            f"/api/threads/{thread}/messages",
            headers={"Idempotency-Key": str(uuid4())},
            json={"body": f"合成留言 {index}"},
        )
        assert response.status_code == 201
    return thread, RunStore(client.app.state.database)


def result(store, run):
    return ReplyResult.model_validate(
        {
            "replies": [
                {
                    "message_id": message["id"],
                    "body": "合成回信，用于事务验证。",
                }
                for message in store.snapshot(run)["selected"]
            ]
        }
    )


def test_scope_snapshot_and_atomic_publish(client):
    thread, store = pending(client)
    run = store.queue(thread, 3600)
    client.post(
        f"/api/threads/{thread}/messages",
        headers={"Idempotency-Key": str(uuid4())},
        json={"body": "运行中新增"},
    )
    assert len(store.snapshot(run)["selected"]) == 2
    store.started(run)
    assert store.publish(run, result(store, run))
    assert not store.publish(run, result(store, run))
    messages = client.get(f"/api/threads/{thread}/messages").json()["messages"]
    assert [item["role"] for item in messages] == ["visitor"] * 3 + ["milo"] * 2
    assert client.get("/api/threads").json()[0]["pending_count"] == 1
    assert store.get(run)["status"] == "succeeded"


def test_cancel_and_deadline_block_publication(client):
    thread, store = pending(client)
    run = store.queue(thread, 3600)
    store.started(run)
    store.stop(run)
    assert not store.publish(run, result(store, run))
    store.finish_failed(run, "irrelevant")
    assert store.get(run)["status"] == "cancelled"
    second = store.queue(thread, 1)
    store.started(second)
    deadline = store.get(second)["deadline_at"]
    store.clock = lambda: deadline
    assert not store.publish(second, result(store, second))
    store.finish_failed(second, "irrelevant")
    assert store.get(second)["status"] == "timed_out"


def test_single_active_claim_and_invalid_result(client):
    thread, store = pending(client)

    def claim():
        try:
            return store.queue(thread, 3600)
        except ApiError:
            return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(lambda _: claim(), range(4)))
    assert sum(item is not None for item in claims) == 1
    run = next(item for item in claims if item)
    store.started(run)
    bad = result(store, run)
    bad.replies[0].message_id = uuid4()
    with pytest.raises(ValueError):
        store.publish(run, bad)
    assert store.get(run)["status"] == "running"
    assert client.get("/api/threads").json()[0]["pending_count"] == 2
    with store.database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO runs (id, thread_id, status, created_at) VALUES (?, ?, 'queued', 0)",
                (str(uuid4()), thread),
            )


def test_result_scope_duplicates_and_timeout_bounds(client):
    thread, store = pending(client, 1)
    for timeout in [0, 3601]:
        with pytest.raises(ValueError):
            store.queue(thread, timeout)
    run = store.queue(thread, 3600)
    replies = result(store, run)
    replies.replies.append(replies.replies[0])
    with pytest.raises(ValueError):
        replies.require_scope([store.snapshot(run)["selected"][0]["id"]])


def test_run_api_is_owner_only_and_does_not_expose_unit(client):
    thread, store = pending(client, 1)
    run = store.queue(thread, 3600)
    rows = client.get(f"/api/threads/{thread}/runs").json()
    assert rows[0]["id"] == run
    assert "unit_name" not in rows[0] and "stop_reason" not in rows[0]
    activate(client, "other")
    assert client.get(f"/api/threads/{thread}/runs").status_code == 404
    assert client.get(f"/api/threads/{uuid4()}/runs").status_code == 404


def test_latest_run_order_for_equal_creation_times(client):
    thread, store = pending(client, 1)
    store.clock = lambda: 1000
    first = store.queue(thread, 60)
    store.finish_failed(first, "execution_failed")
    second = store.queue(thread, 60)
    rows = client.get(f"/api/threads/{thread}/runs").json()
    assert [row["id"] for row in rows] == [second, first]

from uuid import uuid4

import pytest
from conftest import activate, own_thread
from test_conversations import create_letter, send
from test_runs import result


@pytest.fixture
def history(client):
    user, _ = activate(client)
    thread = own_thread(client)
    chat = client.put(f"/api/threads/{thread}/chat").json()["id"]
    database = client.app.state.conversations.database
    # Interleave another conversation to expose thread-global cursor gaps.
    with database.connect(write=True) as connection:
        connection.executemany(
            "INSERT INTO messages (id, thread_id, conversation_id, sequence, author_id, "
            "role, body, client_key, created_at) VALUES (?, ?, ?, ?, ?, 'visitor', ?, ?, 1000)",
            [
                (
                    str(uuid4()),
                    thread,
                    chat if sequence % 2 == 0 else thread,
                    sequence,
                    user.json()["id"],
                    f"合成纸条 {sequence}",
                    str(uuid4()),
                )
                for sequence in range(1, 901)
            ],
        )
    return thread, chat


def test_latest_page_then_earlier_pages_are_bounded_and_complete(client, history):
    _, chat = history
    endpoint = f"/api/conversations/{chat}/messages"
    page = client.get(endpoint, params={"latest": True, "limit": 100}).json()
    assert [row["sequence"] for row in page["messages"]] == list(range(900, 700, -2))
    assert page["next_after"] == 900
    assert page["next_before"] == 702
    assert page["has_more_before"] is True
    sequences = [row["sequence"] for row in page["messages"]]
    while page["has_more_before"]:
        page = client.get(endpoint, params={"before": page["next_before"], "limit": 100}).json()
        assert 1 <= len(page["messages"]) <= 100
        sequences.extend(row["sequence"] for row in page["messages"])
    assert sequences == list(range(900, 0, -2))
    assert page["next_before"] == 2
    empty = client.get(endpoint, params={"before": 2}).json()
    assert empty == {
        "messages": [],
        "next_after": 0,
        "next_before": None,
        "has_more_before": False,
    }


def test_new_arrival_between_earlier_pages_is_found_by_original_high_water(client, history):
    _, chat = history
    endpoint = f"/api/conversations/{chat}/messages"
    latest = client.get(endpoint, params={"latest": True, "limit": 2}).json()
    posted = send(client, chat, "最新的一张合成纸条").json()
    earlier = client.get(endpoint, params={"before": latest["next_before"], "limit": 2}).json()
    assert [row["sequence"] for row in earlier["messages"]] == [896, 894]
    arrivals = client.get(endpoint, params={"after": latest["next_after"]}).json()
    assert [row["id"] for row in arrivals["messages"]] == [posted["id"]]
    assert arrivals["next_after"] == posted["sequence"]
    assert arrivals["has_more_before"] is True


@pytest.mark.parametrize(
    "parameters",
    [
        {"after": 0, "latest": True},
        {"after": 0, "before": 10},
        {"before": 10, "latest": True},
        {"before": 0},
        {"before": -1},
        {"before": 2**63},
        {"after": 2**63},
        {"limit": 101},
    ],
)
def test_invalid_or_mixed_pagination_is_refused(client, parameters):
    activate(client)
    thread = own_thread(client)
    response = client.get(f"/api/conversations/{thread}/messages", params=parameters)
    assert response.status_code == 422


def test_empty_latest_and_legacy_forward_page(client):
    activate(client)
    thread = own_thread(client)
    endpoint = f"/api/conversations/{thread}/messages"
    latest = client.get(endpoint, params={"latest": True}).json()
    assert latest["messages"] == [] and latest["next_after"] == 0
    assert latest["next_before"] is None and not latest["has_more_before"]
    send(client, thread, "合成第一页")
    send(client, thread, "合成第二页")
    default = client.get(endpoint).json()
    assert [row["sequence"] for row in default["messages"]] == [1, 2]
    assert default["next_before"] == 1 and not default["has_more_before"]
    assert client.get(endpoint, params={"after": 50}).json()["next_after"] == 50


def test_reverse_pages_and_summaries_keep_account_isolation(client, history):
    thread, chat = history
    activate(client, "other")
    for parameters in ({"latest": True}, {"before": 900}):
        hidden = client.get(f"/api/conversations/{chat}/messages", params=parameters)
        missing = client.get(f"/api/conversations/{uuid4()}/messages", params=parameters)
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()
    assert client.get(f"/api/threads/{thread}/conversations").status_code == 404
    client.post("/api/auth/logout")
    assert (
        client.get(f"/api/conversations/{chat}/messages", params={"latest": True}).status_code
        == 401
    )


def test_summary_uses_saved_messages_and_distinguishes_empty_replied_and_pending(client):
    activate(client)
    thread = own_thread(client)
    empty = create_letter(client, thread, "空白合成信").json()
    assert empty["message_count"] == 0 and empty["pending_count"] == 0
    assert all(
        empty[field] is None for field in ("last_message_at", "last_message_role", "last_reply_at")
    )
    send(client, thread, "等回信的合成正文")
    from milo_home.runs import RunStore

    store = RunStore(client.app.state.conversations.database, clock=lambda: 2000)
    run = store.queue(thread, 60)
    store.started(run)
    assert store.publish(run, result(store, run))
    rows = client.get(f"/api/threads/{thread}/conversations").json()
    summary = next(row for row in rows if row["id"] == thread)
    assert summary["message_count"] == 2 and summary["pending_count"] == 0
    assert summary["last_message_role"] == "milo"
    assert summary["last_message_at"] == summary["last_reply_at"] == 2000
    send(client, thread, "继续写的合成正文")
    # Same-second timestamps do not determine which role is last.
    with store.database.connect(write=True) as connection:
        connection.execute("UPDATE messages SET created_at = 2000 WHERE thread_id = ?", (thread,))
    summary = next(
        row
        for row in client.get(f"/api/threads/{thread}/conversations").json()
        if row["id"] == thread
    )
    assert summary["message_count"] == 3 and summary["pending_count"] == 1
    assert summary["last_message_role"] == "visitor" and summary["last_reply_at"] == 2000
    assert "body" not in summary and "author_id" not in summary

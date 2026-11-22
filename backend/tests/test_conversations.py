from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from conftest import ORIGIN, activate, own_thread
from fastapi.testclient import TestClient

from milo_home.app import create_app


def create_letter(client, thread, title=None, key=None):
    return client.post(
        f"/api/threads/{thread}/conversations",
        json={"title": title},
        headers={"Idempotency-Key": key or str(uuid4())},
    )


def send(client, conversation, body, key=None):
    return client.post(
        f"/api/conversations/{conversation}/messages",
        json={"body": body},
        headers={"Idempotency-Key": key or str(uuid4())},
    )


def test_default_letter_and_idempotent_chat_creation(client):
    user, _ = activate(client)
    thread = own_thread(client)
    rows = client.get(f"/api/threads/{thread}/conversations").json()
    assert len(rows) == 1 and rows[0]["is_default"]
    assert rows[0]["kind"] == "letter" and rows[0]["title"] is None
    service = client.app.state.conversations
    with ThreadPoolExecutor(max_workers=4) as pool:
        chats = list(pool.map(lambda _: service.chat(user.json()["id"], thread), range(4)))
    assert len({chat["id"] for chat in chats}) == 1
    assert client.put(f"/api/threads/{thread}/chat").json()["id"] == chats[0]["id"]
    assert client.get("/api/threads").json()[0]["pending_count"] == 0
    assert client.get(f"/api/threads/{thread}/runs").json() == []


def test_letter_creation_normalization_and_conflicting_retry(client):
    activate(client)
    thread = own_thread(client)
    key = str(uuid4())
    first = create_letter(client, thread, " 合成主题 ", key)
    repeat = create_letter(client, thread, "合成主题", key)
    assert first.status_code == 201 and repeat.status_code == 200
    assert first.json() == repeat.json()
    assert first.json()["title"] == "合成主题"
    assert "client_key" not in first.json()
    assert create_letter(client, thread, "其他主题", key).status_code == 409
    assert create_letter(client, thread, " ").status_code == 422
    assert create_letter(client, thread, "字" * 81).status_code == 422


def test_legacy_post_and_new_destination_share_idempotency_and_history(client):
    activate(client)
    thread = own_thread(client)
    default = client.get(f"/api/threads/{thread}/conversations").json()[0]["id"]
    chat = client.put(f"/api/threads/{thread}/chat").json()["id"]
    key = str(uuid4())
    old = client.post(
        f"/api/threads/{thread}/messages",
        json={"body": "旧入口的信"},
        headers={"Idempotency-Key": key},
    )
    repeat = send(client, default, "旧入口的信", key)
    assert old.status_code == 201 and repeat.status_code == 200
    assert old.json()["id"] == repeat.json()["id"]
    assert repeat.json()["conversation_id"] == default
    assert send(client, chat, "旧入口的信", key).status_code == 409
    assert send(client, chat, "合成聊天").status_code == 201
    assert send(client, default, "续信").status_code == 201
    endpoint = f"/api/conversations/{default}/messages"
    first = client.get(endpoint, params={"limit": 1}).json()
    second = client.get(endpoint, params={"after": first["next_after"]}).json()
    assert [first["messages"][0]["sequence"], second["messages"][0]["sequence"]] == [1, 3]
    empty = client.get(endpoint, params={"after": 3}).json()
    assert empty == {
        "messages": [],
        "next_after": 3,
        "next_before": None,
        "has_more_before": False,
    }
    with TestClient(create_app(client.app.state.settings), base_url=ORIGIN) as reopened:
        reopened.cookies.update(client.cookies)
        assert len(reopened.get(endpoint).json()["messages"]) == 2
        assert len(reopened.get(f"/api/threads/{thread}/messages").json()["messages"]) == 3


def test_concurrent_letter_and_message_retries_create_once(client):
    user, _ = activate(client)
    owner, thread = user.json()["id"], own_thread(client)
    service = client.app.state.conversations
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        letters = list(
            pool.map(lambda _: service.create_letter(owner, thread, None, key), range(4))
        )
    assert sum(created for _, created in letters) == 1
    conversation = letters[0][0]["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        messages = list(
            pool.map(lambda _: service.post(owner, conversation, "并发文本", key), range(4))
        )
    assert sum(created for _, created in messages) == 1
    assert len({message["id"] for message, _ in messages}) == 1


def test_private_conversations_cannot_be_read_or_written_by_other_accounts(client):
    activate(client)
    thread = own_thread(client)
    conversation = create_letter(client, thread, "私密主题").json()["id"]
    send(client, conversation, "私密正文")
    activate(client, "other")
    unknown = str(uuid4())
    hidden = client.get(f"/api/conversations/{conversation}/messages")
    missing = client.get(f"/api/conversations/{unknown}/messages")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    assert send(client, conversation, "越界").status_code == 404
    assert client.get(f"/api/threads/{thread}/conversations").status_code == 404
    assert create_letter(client, thread).status_code == 404
    assert client.put(f"/api/threads/{thread}/chat").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {
            "body": "空",
            "role": "milo",
        },
        {"body": " "},
    ],
)
def test_conversation_message_validation_and_origin(client, body):
    activate(client)
    thread = own_thread(client)
    chat = client.put(f"/api/threads/{thread}/chat").json()["id"]
    endpoint = f"/api/conversations/{chat}/messages"
    assert (
        client.post(endpoint, json=body, headers={"Idempotency-Key": str(uuid4())}).status_code
        == 422
    )
    assert client.post(endpoint, json={"body": "x"}).status_code == 422
    assert client.get(endpoint, params={"limit": 101}).status_code == 422
    assert (
        client.post(
            endpoint,
            json={"body": "x"},
            headers={
                "Origin": "https://other.test",
                "Idempotency-Key": str(uuid4()),
            },
        ).status_code
        == 403
    )
    client.post("/api/auth/logout")
    assert client.get(endpoint).status_code == 401

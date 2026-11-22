from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from conftest import ORIGIN, activate, own_thread
from fastapi.testclient import TestClient

from milo_home.app import create_app


def test_private_thread_isolation(client):
    activate(client, "firstuser")
    first = own_thread(client)
    client.post("/api/auth/logout")
    activate(client, "seconduser")
    assert own_thread(client) != first
    unknown = str(uuid4())
    hidden = client.get(f"/api/threads/{first}/messages")
    missing = client.get(f"/api/threads/{unknown}/messages")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    response = client.post(
        f"/api/threads/{first}/messages",
        json={"body": "尝试跨账号写入"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 404
    assert client.get("/api/threads").json()[0]["pending_count"] == 0


def test_persistence_idempotency_and_pagination(client):
    activate(client)
    thread = own_thread(client)
    key = str(uuid4())
    endpoint = f"/api/threads/{thread}/messages"
    first = client.post(
        endpoint, json={"body": " 第一条测试留言 "}, headers={"Idempotency-Key": key}
    )
    repeated = client.post(
        endpoint, json={"body": "第一条测试留言"}, headers={"Idempotency-Key": key}
    )
    assert first.status_code == 201 and repeated.status_code == 200
    assert first.json() == repeated.json()
    conflict = client.post(endpoint, json={"body": "不同内容"}, headers={"Idempotency-Key": key})
    assert conflict.status_code == 409
    client.post(
        endpoint, json={"body": "第二条测试留言"}, headers={"Idempotency-Key": str(uuid4())}
    )
    first_page = client.get(endpoint, params={"limit": 1}).json()
    second_page = client.get(endpoint, params={"after": first_page["next_after"]}).json()
    assert len(first_page["messages"]) == len(second_page["messages"]) == 1
    assert first_page["messages"][0]["role"] == "visitor"
    assert first_page["messages"][0]["in_reply_to"] is None
    assert client.get("/api/threads").json()[0]["pending_count"] == 2
    with TestClient(create_app(client.app.state.settings), base_url=ORIGIN) as reopened:
        reopened.cookies.update(client.cookies)
        assert len(reopened.get(endpoint).json()["messages"]) == 2


def test_message_input_cannot_spoof_reply_or_author(client):
    activate(client)
    endpoint = f"/api/threads/{own_thread(client)}/messages"
    for body in [
        {"body": "   "},
        {
            "body": "x",
            "role": "milo",
        },
        {
            "body": "x",
            "author_id": str(uuid4()),
        },
    ]:
        response = client.post(endpoint, json=body, headers={"Idempotency-Key": str(uuid4())})
        assert response.status_code == 422
    assert client.post(endpoint, json={"body": "缺少请求标识"}).status_code == 422
    assert client.get(endpoint, params={"limit": 101}).status_code == 422
    assert client.get(endpoint).json()["messages"] == []


def test_concurrent_same_key_creates_one_message(client):
    user, _ = activate(client)
    thread = own_thread(client)
    service = client.app.state.messages
    key = str(uuid4())

    def post():
        return service.post(user.json()["id"], thread, "并发重试测试", key)

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: post(), range(4)))
    assert sum(created for _, created in outcomes) == 1
    assert len({message["id"] for message, _ in outcomes}) == 1


def test_anonymous_access_is_rejected(client):
    assert client.get("/api/threads").status_code == 401
    assert client.get(f"/api/threads/{uuid4()}/messages").status_code == 401
    assert client.get("/api/health").json()["automatic_wakeup"] is False

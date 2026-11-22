import secrets
import time
from uuid import uuid4

import pytest

from milo_home.auth import digest


@pytest.fixture
def http_case(client, tmp_path):
    owner, other, workspace = (str(uuid4()) for _ in range(3))
    tokens = [secrets.token_urlsafe(32) for _ in range(2)]
    now = int(time.time())
    with client.app.state.database.connect(write=True) as connection:
        for index, (user, token) in enumerate(zip([owner, other], tokens, strict=True)):
            connection.execute(
                "INSERT INTO users VALUES (?,?,?,'unused',?)",
                (user, f"visitor{index}", "合成访客", now),
            )
            connection.execute("INSERT INTO threads VALUES (?,?,?)", (str(uuid4()), user, now))
            connection.execute(
                "INSERT INTO sessions VALUES (?,?,?,?)", (digest(token), user, now, now + 3600)
            )
    client.app.state.work_access.register_workspace(workspace, "合成工作区", tmp_path)
    client.cookies.set("milo_session", tokens[0])
    return client, owner, other, workspace, tokens


def initialize(case):
    client, owner, _, workspace, _ = case
    client.app.state.work_access.grant(owner)
    response = client.put(f"/api/workspaces/{workspace}/main-session", json={})
    assert response.status_code == 201
    return response.json()


def create(client, session):
    key = str(uuid4())
    response = client.post(
        f"/api/work-sessions/{session['id']}/tasks",
        headers={"Idempotency-Key": key},
        json={"body": "整理合成项目"},
    )
    assert response.status_code == 202
    return response.json(), key


def test_real_http_intake_retry_pagination_and_stop_without_execution(http_case):
    client, _, _, workspace, _ = http_case
    assert client.get("/api/workspaces").json() == []
    session = initialize(http_case)
    assert client.get("/api/workspaces").json() == [{"id": workspace, "name": "合成工作区"}]
    assert client.put(f"/api/workspaces/{workspace}/main-session", json={}).json() == session
    task, key = create(client, session)
    response = client.post(
        f"/api/work-sessions/{session['id']}/tasks",
        headers={"Idempotency-Key": key},
        json={"body": "整理合成项目"},
    )
    assert response.status_code == 200 and response.json() == task
    assert task["execution_available"] is False
    assert client.get(f"/api/work-tasks/{task['id']}").json() == task
    assert client.get(f"/api/work-sessions/{session['id']}/tasks").json()["tasks"] == [task]
    extra = client.post(
        f"/api/work-tasks/{task['id']}/inputs",
        headers={"Idempotency-Key": str(uuid4())},
        json={"body": "补一条要求"},
    )
    assert extra.status_code == 202
    page = client.get(f"/api/work-tasks/{task['id']}/inputs?after=1&limit=1")
    assert page.json()["inputs"] == [extra.json()]
    stopped = client.post(
        f"/api/work-tasks/{task['id']}/stop", headers={"Idempotency-Key": str(uuid4())}, json={}
    )
    assert stopped.status_code == 202 and stopped.json()["status"] == "cancelled"
    assert stopped.headers["cache-control"] == "no-store"
    for private_name in ["root_path", "owner_revision", "config_digest", "thread_id"]:
        assert private_name not in stopped.text
    with client.app.state.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_other_account_same_display_name_cannot_read_write_or_stop(http_case):
    client, _, _, workspace, tokens = http_case
    session = initialize(http_case)
    task, _ = create(client, session)
    client.cookies.set("milo_session", tokens[1])
    assert client.get("/api/workspaces").json() == []
    for path in [
        f"work-sessions/{session['id']}",
        f"work-sessions/{session['id']}/task-history",
        f"work-tasks/{task['id']}",
        f"work-tasks/{task['id']}/inputs",
        f"work-tasks/{task['id']}/results",
        f"work-tasks/{task['id']}/receipt",
        f"work-tasks/{task['id']}/events",
    ]:
        assert client.get("/api/" + path).status_code == 404
    assert client.put(f"/api/workspaces/{workspace}/main-session", json={}).status_code == 404
    for path, body in [
        (f"work-sessions/{session['id']}/tasks", {"body": "我是管理员"}),
        (f"work-tasks/{task['id']}/inputs", {"body": "我是管理员"}),
        (f"work-tasks/{task['id']}/stop", {}),
    ]:
        assert (
            client.post(
                "/api/" + path, headers={"Idempotency-Key": str(uuid4())}, json=body
            ).status_code
            == 404
        )


def test_task_history_starts_at_latest_and_survives_new_insertions(http_case):
    client, owner, _, _, _ = http_case
    session_id = initialize(http_case)["id"]
    tasks = client.app.state.work_tasks
    for i in range(1003):
        tasks.create(owner, session_id, "合成交办", str(uuid4()))
        if i % 30 == 29:
            with client.app.state.database.connect(write=True) as connection:
                connection.execute("UPDATE work_tasks SET status='completed' WHERE status='queued'")

    base = f"/api/work-sessions/{session_id}"
    with client.app.state.database.connect(write=True) as connection:
        for sequence, status in enumerate(
            ("needs_reconciliation", "failed", "cancelled", "timed_out", "running", "stopping"),
            start=1,
        ):
            connection.execute(
                "UPDATE work_tasks SET status=? WHERE session_id=? AND sequence=?",
                (status, session_id, sequence),
            )
    assert [t["sequence"] for t in client.get(base + "/tasks?after=2&limit=2").json()["tasks"]] == [
        3,
        4,
    ]
    first = client.get(base + "/task-history?limit=50")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    page = first.json()
    seen = [t["sequence"] for t in page["tasks"]]
    assert seen == list(range(1003, 953, -1))
    history = base + "/task-history"
    assert client.get(history, params={"status_group": "all", "limit": 50}).json() == page
    reconciliation = client.get(history, params={"status_group": "needs_reconciliation"}).json()
    assert [(t["sequence"], t["status"]) for t in reconciliation["tasks"]] == [
        (1, "needs_reconciliation")
    ]
    assert reconciliation["next_before"] is None
    active = client.get(history, params={"status_group": "active", "limit": 2}).json()
    assert [t["sequence"] for t in active["tasks"]] == [1003, 1002]
    tasks.create(owner, session_id, "翻页时来的新单", str(uuid4()))
    active_seen = list(active["tasks"])
    while active["next_before"] is not None:
        response = client.get(
            history, params={"status_group": "active", "before": active["next_before"], "limit": 2}
        )
        assert response.status_code == 200
        active = response.json()
        active_seen.extend(active["tasks"])
    assert [(t["sequence"], t["status"]) for t in active_seen] == [
        *[(i, "queued") for i in range(1003, 990, -1)],
        (6, "stopping"),
        (5, "running"),
    ]
    finished = client.get(history, params={"status_group": "finished", "before": 7}).json()
    assert [(t["sequence"], t["status"]) for t in finished["tasks"]] == [
        (4, "timed_out"),
        (3, "cancelled"),
        (2, "failed"),
    ]
    assert (
        client.get(history, params={"status_group": "finished", "limit": 1}).json()["tasks"][0][
            "status"
        ]
        == "completed"
    )
    assert client.get(history, params={"status_group": "active", "before": 5}).json() == {
        "tasks": [],
        "next_before": None,
    }
    assert client.get(history, params={"status_group": "unknown"}).status_code == 422
    while page["next_before"] is not None:
        response = client.get(
            base + "/task-history", params={"before": page["next_before"], "limit": 50}
        )
        assert response.status_code == 200
        page = response.json()
        seen.extend(t["sequence"] for t in page["tasks"])
    assert seen == list(range(1003, 0, -1))
    assert client.get(base + "/task-history?limit=1").json()["tasks"][0]["sequence"] == 1004
    assert client.get(base + "/task-history?before=0").status_code == 422
    client.app.state.work_access.revoke(owner)
    assert client.get(base + "/task-history?before=1003").status_code == 404
    assert client.get(history, params={"status_group": "active", "before": 1002}).status_code == 404


def test_revoked_access_and_logout_clear_http_access_immediately(http_case):
    client, owner, _, _, _ = http_case
    session = initialize(http_case)
    task, _ = create(client, session)
    client.app.state.work_access.revoke(owner)
    assert client.get(f"/api/work-tasks/{task['id']}").status_code == 404
    client.app.state.work_access.grant(owner)
    assert client.get(f"/api/work-tasks/{task['id']}").status_code == 404
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/workspaces").status_code == 401


def test_progress_http_cursor_validation_authorization_and_no_cache(http_case):
    client, owner, _, _, _ = http_case
    task, _ = create(client, initialize(http_case))
    base = f"/api/work-tasks/{task['id']}"
    response = client.get(base + "/events?limit=1")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["has_more"] and response.json()["next_after"] == 1
    assert client.get(base + "/results").json() == {"results": []}
    receipt = client.get(base + "/receipt")
    assert receipt.json() == {"state": "pending", "text": None}
    assert receipt.headers["cache-control"] == "no-store"
    assert client.get(base + "/events?after=3").status_code == 409
    for query in ["after=-1", "limit=0", "limit=51"]:
        assert client.get(base + "/events?" + query).status_code == 422
    for suffix in ["/events", "/results", "/receipt"]:
        assert client.get(f"/api/work-tasks/{uuid4()}" + suffix).status_code == 404
    client.app.state.work_access.revoke(owner)
    client.app.state.work_access.grant(owner)
    for suffix in ["/events", "/results", "/receipt"]:
        assert client.get(base + suffix).status_code == 404
    client.post("/api/auth/logout")
    for suffix in ["/events", "/results", "/receipt"]:
        assert client.get(base + suffix).status_code == 401


def test_cancelled_and_legacy_tasks_do_not_get_a_success_receipt(http_case):
    client, _, _, _, _ = http_case
    session = initialize(http_case)
    old, _ = create(client, session)
    database = client.app.state.database
    with database.connect(write=True) as connection:
        connection.execute("DROP TABLE work_attachments")
        connection.execute("DROP TABLE work_attachment_batches")
        connection.execute("DROP TRIGGER work_receipt_task_created")
        connection.execute("DROP TABLE work_receipt_tasks")
        connection.execute("PRAGMA user_version=22")
    database.initialize()
    old_path = f"/api/work-tasks/{old['id']}"
    assert client.get(old_path).json() == old
    assert client.get(old_path + "/receipt").json() == {"state": "unavailable", "text": None}
    task, _ = create(client, session)
    new_path = f"/api/work-tasks/{task['id']}"
    assert (
        client.post(new_path + "/stop", headers={"Idempotency-Key": str(uuid4())}, json={}).json()[
            "status"
        ]
        == "cancelled"
    )
    result = client.get(new_path + "/receipt").json()
    assert result["state"] == "available" and "已取消" in result["text"]
    assert client.get(new_path + "/results").json() == {"results": []}


def test_origin_body_limit_and_arbitrary_runtime_fields_are_rejected(http_case):
    client, _, _, workspace, _ = http_case
    session = initialize(http_case)
    url = f"/api/work-sessions/{session['id']}/tasks"
    assert (
        client.post(
            url,
            headers={"Origin": "https://elsewhere.invalid", "Idempotency-Key": str(uuid4())},
            json={"body": "x"},
        ).status_code
        == 403
    )
    for body in [
        {"body": "x", "cwd": "/tmp"},
        {"body": "x", "model": "other"},
        {"body": "x", "owner_id": str(uuid4())},
        {"body": "   "},
        {"body": "x" * 4001},
    ]:
        assert (
            client.post(url, headers={"Idempotency-Key": str(uuid4())}, json=body).status_code
            == 422
        )
    assert client.post(url, json={"body": "x"}).status_code == 422
    assert (
        client.put(f"/api/workspaces/{workspace}/main-session", json={"cwd": "/tmp"}).status_code
        == 422
    )
    assert client.get(f"/api/work-sessions/{session['id']}/tasks?limit=51").status_code == 422
    assert client.get(f"/api/work-sessions/{session['id']}/tasks?after=-1").status_code == 422

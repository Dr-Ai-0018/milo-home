from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from private_test_runtime import private_test_runtime

from milo_home.app import create_app
from milo_home.config import Settings

ORIGIN = "https://home.test"
PASSWORD = "synthetic-test-password-42"


@pytest.fixture
def private_runtime_directory():
    with ExitStack() as stack:
        yield lambda prefix: stack.enter_context(private_test_runtime(prefix))


@pytest.fixture
def client(tmp_path):
    settings = Settings(database=tmp_path / "home.sqlite3", origins=(ORIGIN,))
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        client.headers["Origin"] = ORIGIN
        yield client


def activate(client, handle="visitor", *, token=None):
    token = token or client.app.state.auth.create_invitation(handle)
    response = client.post(
        "/api/auth/accept-invite",
        json={
            "token": token,
            "password": PASSWORD,
            "display_name": "测试来访者",
        },
    )
    assert response.status_code == 201
    return response, token


def own_thread(client):
    response = client.get("/api/threads")
    assert response.status_code == 200
    return response.json()[0]["id"]

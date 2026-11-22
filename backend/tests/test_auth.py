import secrets
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import ORIGIN, PASSWORD, activate
from fastapi.testclient import TestClient

from milo_home.app import create_app
from milo_home.config import Settings
from milo_home.errors import ApiError


def test_activation_cookie_login_and_logout(client):
    response, token = activate(client)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "token" not in response.json() and "password_hash" not in response.json()
    assert client.get("/api/auth/me").json()["handle"] == "visitor"
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    response = client.post(
        "/api/auth/login",
        json={
            "handle": "visitor",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 200
    assert client.get("/api/auth/me").status_code == 200
    assert (
        client.post(
            "/api/auth/accept-invite",
            json={
                "token": token,
                "password": PASSWORD,
                "display_name": "重复激活",
            },
        ).status_code
        == 401
    )


def test_secrets_are_hashed_and_validation_does_not_echo_inputs(client):
    _, token = activate(client)
    session_token = client.cookies.get("milo_session")
    with client.app.state.database.connect() as connection:
        user = connection.execute("SELECT * FROM users").fetchone()
        invitation = connection.execute("SELECT * FROM invitations").fetchone()
        session = connection.execute("SELECT * FROM sessions").fetchone()
    assert user["password_hash"].startswith("$argon2id$")
    assert PASSWORD not in user["password_hash"]
    assert token not in invitation["token_hash"]
    assert session_token not in session["token_hash"]
    response = client.post(
        "/api/auth/login",
        json={
            "handle": "invalid handle",
            "password": PASSWORD,
            "extra_secret": token,
        },
    )
    assert response.status_code == 422
    assert PASSWORD not in response.text and token not in response.text


def test_revoked_and_expired_sessions_are_rejected(client):
    activate(client)
    saved_token = client.cookies.get("milo_session")
    assert client.post("/api/auth/logout").status_code == 204
    client.cookies.set("milo_session", saved_token)
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.clear()
    assert (
        client.post(
            "/api/auth/login",
            json={
                "handle": "visitor",
                "password": PASSWORD,
            },
        ).status_code
        == 200
    )
    with client.app.state.database.connect(write=True) as connection:
        connection.execute("UPDATE sessions SET expires_at = 0")
    assert client.get("/api/auth/me").status_code == 401


def test_wrong_account_and_password_share_error(client):
    activate(client)
    unknown = client.post(
        "/api/auth/login",
        json={
            "handle": "absent",
            "password": PASSWORD,
        },
    )
    wrong = client.post(
        "/api/auth/login",
        json={
            "handle": "visitor",
            "password": "another-synthetic-password",
        },
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_invitation_expiry_and_concurrent_consumption(client):
    service = client.app.state.auth
    expired = service.create_invitation("expired", ttl_seconds=-1)
    with pytest.raises(ApiError) as failure:
        service.activate(expired, PASSWORD, "过期邀请")
    assert failure.value.code == "invitation_invalid"
    token = service.create_invitation("raceuser")

    def consume():
        try:
            service.activate(token, PASSWORD, "并发激活")
            return "created"
        except ApiError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: consume(), range(2)))
    assert sorted(outcomes) == ["created", "invitation_invalid"]
    with service.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_origin_host_and_body_limits(client):
    response = client.post("/api/auth/logout", headers={"Origin": "https://outside.test"})
    assert response.status_code == 403
    del client.headers["Origin"]
    assert client.post("/api/auth/logout").status_code == 403
    client.headers["Origin"] = ORIGIN
    assert client.get("/api/health", headers={"Host": "outside.test"}).status_code == 400
    response = client.post("/api/auth/login", content=b"x" * 17000)
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"


def test_auth_attempt_limit_persists_across_restart(tmp_path):
    settings = Settings(
        database=tmp_path / "rate.sqlite3",
        origins=(ORIGIN,),
        auth_attempts=2,
    )
    payload = {
        "token": secrets.token_urlsafe(32),
        "password": PASSWORD,
        "display_name": "限流测试",
    }
    for expected in [401, 429]:
        with TestClient(create_app(settings), base_url=ORIGIN) as client:
            client.headers["Origin"] = ORIGIN
            if expected == 401:
                assert client.post("/api/auth/accept-invite", json=payload).status_code == 401
            response = client.post("/api/auth/accept-invite", json=payload)
            assert response.status_code == expected


def test_openapi_matches_cookie_and_sanitized_error_contract(client):
    schema = client.get("/openapi.json").json()
    security = schema["components"]["securitySchemes"]["APIKeyCookie"]
    assert security["in"] == "cookie" and security["name"] == "milo_session"
    assert schema["paths"]["/api/threads"]["get"]["security"] == [{"APIKeyCookie": []}]
    errors = schema["paths"]["/api/auth/login"]["post"]["responses"]["422"]
    assert errors["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorEnvelope")

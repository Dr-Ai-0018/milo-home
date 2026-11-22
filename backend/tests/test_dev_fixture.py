import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from milo_home.app import create_app
from milo_home.config import Settings
from milo_home.database import Database
from milo_home.dev_fixture import create_fixture

ORIGIN = "http://127.0.0.1:7473"


@pytest.mark.parametrize(
    "scenario", ["replied", "pending", "failed", "cancelled", "timed_out", "dual-entry"]
)
def test_fixture_works_with_real_api(tmp_path, scenario):
    directory = create_fixture(tmp_path, scenario)
    details = json.loads((directory / "login.json").read_text())
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "login.json").stat().st_mode & 0o777 == 0o600
    assert (directory / "home.sqlite3").stat().st_mode & 0o777 == 0o600
    assert (directory / "SYNTHETIC-PREVIEW.txt").read_text().startswith("READY")
    settings = Settings(database=directory / "home.sqlite3", origins=(ORIGIN,), secure_cookie=False)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        client.headers["Origin"] = ORIGIN
        response = client.post(
            "/api/auth/login", json={"handle": details["handle"], "password": details["password"]}
        )
        assert response.status_code == 200
        assert "合成样例" in response.json()["display_name"]
        thread = client.get("/api/threads").json()[0]
        pending = 4 if scenario == "dual-entry" else 2 if scenario == "pending" else 1
        assert thread["pending_count"] == pending
        path = f"/api/threads/{thread['id']}"
        response = client.get(path + "/messages")
        assert response.status_code == 200
        messages = response.json()["messages"]
        assert all("合成" in message["body"] for message in messages)
        replies = [message for message in messages if message["role"] == "milo"]
        expected_replies = 5 if scenario == "dual-entry" else 0 if scenario == "pending" else 2
        assert len(replies) == expected_replies
        incoming = {message["id"] for message in messages if message["role"] == "visitor"}
        assert all(message["in_reply_to"] in incoming for message in replies)
        runs = client.get(path + "/runs").json()
        if scenario == "pending":
            assert runs == []
        else:
            expected = "succeeded" if scenario in {"replied", "dual-entry"} else scenario
            assert runs[0]["status"] == expected
        if scenario == "dual-entry":
            conversations = client.get(path + "/conversations").json()
            assert len(conversations) == 4
            assert sum(row["kind"] == "chat" for row in conversations) == 1
            assert sum(row["title"] is not None for row in conversations) == 2
            assert all(row["pending_count"] == 1 for row in conversations)
            aggregate_ids = set()
            for conversation in conversations:
                url = f"/api/conversations/{conversation['id']}"
                page = client.get(url + "/messages").json()
                rows = page["messages"]
                assert all(row["conversation_id"] == conversation["id"] for row in rows)
                incoming_ids = {row["id"] for row in rows if row["role"] == "visitor"}
                assert all(
                    row["in_reply_to"] in incoming_ids for row in rows if row["role"] == "milo"
                )
                if conversation["kind"] == "chat":
                    assert len(rows) == 5 and sum(row["role"] == "milo" for row in rows) == 2
                assert client.get(url + "/activity").json()["processing"] == "idle"
                aggregate_ids.update(row["id"] for row in rows)
            assert aggregate_ids == {row["id"] for row in messages}
        schedule = client.get("/api/schedule").json()
        assert schedule["configured"]
        assert schedule["automatic_wakeup"] is False
    database = Database(settings.database)
    with database.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT count(*) FROM runs WHERE status IN ('queued', 'running', 'stopping')"
            ).fetchone()[0]
            == 0
        )


def test_real_cli_ignores_runtime_env_and_creates_new_directories(tmp_path):
    untouched = tmp_path / "existing.sqlite3"
    untouched.write_bytes(b"existing database must not be opened")
    environment = {
        **os.environ,
        "MILO_DATABASE": str(untouched),
        "MILO_COOKIE_SECURE": "invalid-on-purpose",
    }
    for scenario in ["replied", "dual-entry"]:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "milo_home.cli",
                "dev-fixture",
                "--parent",
                str(tmp_path),
                "--scenario",
                scenario,
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        directory = Path(result.stdout.splitlines()[0].removeprefix("Synthetic preview created: "))
        details = json.loads((directory / "login.json").read_text())
        assert details["scenario"] == scenario
        assert details["password"] not in result.stdout + result.stderr
    assert len(list(tmp_path.glob("milo-home-preview-*"))) == 2
    assert untouched.read_bytes() == b"existing database must not be opened"


def test_rejects_invalid_destinations_before_creating_files(tmp_path, monkeypatch):
    protected = tmp_path / "personal"
    protected.mkdir(mode=0o700)
    monkeypatch.setenv("MILO_PROTECTED_ROOTS", str(protected))
    for parent, scenario in [
        (Path("relative"), "replied"),
        (tmp_path / "missing", "replied"),
        (protected, "replied"),
        (tmp_path, "unknown"),
    ]:
        with pytest.raises(ValueError):
            create_fixture(parent, scenario)
    assert list(tmp_path.iterdir()) == [protected]

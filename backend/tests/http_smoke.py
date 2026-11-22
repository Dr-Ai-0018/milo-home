"""Real loopback HTTP smoke with synthetic accounts and a private temporary database."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx2 as httpx


@contextmanager
def server(environment, listener, log):
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "milo_home.app:create_app",
            "--factory",
            "--fd",
            str(listener.fileno()),
            "--no-access-log",
            "--no-proxy-headers",
            "--log-level",
            "error",
        ],
        env=environment,
        pass_fds=[listener.fileno()],
        stdout=log,
        stderr=log,
    )
    try:
        origin = environment["MILO_ORIGINS"]
        for _ in range(50):
            if process.poll() is not None:
                raise RuntimeError("Smoke HTTP server failed to start")
            try:
                response = httpx.get(origin + "/api/health", timeout=0.5, trust_env=False)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Smoke HTTP server readiness timed out")
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    with tempfile.TemporaryDirectory(prefix="milo-home-http-smoke-") as directory:
        private = Path(directory)
        database = private / "home.sqlite3"
        invitation = private / "invitation.json"
        backup = private / "backup.sqlite3"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
            environment = dict(os.environ)
            environment.update(
                MILO_DATABASE=str(database),
                MILO_ORIGINS=origin,
                MILO_COOKIE_SECURE="0",
            )
            command = [sys.executable, "-m", "milo_home.cli"]
            result = subprocess.run(
                command + ["invite", "--handle", "smokeuser", "--out", str(invitation)],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            token = json.loads(invitation.read_text())["token"]
            assert token not in result.stdout + result.stderr
            password = "synthetic-http-smoke-password"
            with (private / "http.log").open("wb") as log:
                with httpx.Client(
                    base_url=origin, headers={"Origin": origin}, trust_env=False
                ) as client:
                    with server(environment, listener, log):
                        activated = client.post(
                            "/api/auth/accept-invite",
                            json={
                                "token": token,
                                "password": password,
                                "display_name": "HTTP 合成测试账号",
                            },
                        )
                        assert activated.status_code == 201
                        assert token not in activated.text and password not in activated.text
                        thread = client.get("/api/threads").json()[0]["id"]
                        endpoint = f"/api/threads/{thread}/messages"
                        key = str(uuid4())
                        headers = {"Idempotency-Key": key}
                        body = {"body": "真实回环 HTTP 的合成留言"}
                        first = client.post(endpoint, json=body, headers=headers)
                        assert first.status_code == 201
                        assert client.post(endpoint, json=body, headers=headers).status_code == 200
                        subprocess.run(
                            command + ["backup", "--out", str(backup)],
                            env=environment,
                            capture_output=True,
                            check=True,
                        )
                    with server(environment, listener, log):
                        page = client.get(endpoint)
                        assert page.status_code == 200
                        assert page.json()["messages"] == [first.json()]
                        assert client.post("/api/auth/logout").status_code == 204
                        assert client.get(endpoint).status_code == 401
                        assert (
                            client.post(
                                "/api/auth/login",
                                json={
                                    "handle": "smokeuser",
                                    "password": password,
                                },
                            ).status_code
                            == 200
                        )
                    environment["MILO_DATABASE"] = str(backup)
                    with server(environment, listener, log):
                        # The independent backup includes the previously issued session.
                        client.cookies.clear()
                        assert (
                            client.post(
                                "/api/auth/login",
                                json={
                                    "handle": "smokeuser",
                                    "password": password,
                                },
                            ).status_code
                            == 200
                        )
                        assert client.get(endpoint).json()["messages"] == [first.json()]
            print(
                "CLI invitation, HTTP session/message, restart and independent backup restore PASS"
            )


if __name__ == "__main__":
    main()

import functools
import http.client
import http.server
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from conftest import PASSWORD
from test_reply_readiness import preparation as preparation
from test_reply_service_entry import service_case as service_case
from test_work_service_entry import wait_for

from milo_home.app import create_app
from milo_home.config import Settings
from milo_home.wakeup_events import EventPlanner

ROOT = Path(__file__).parents[2]
PLAYWRIGHT = Path(os.environ.get("MILO_TEST_PLAYWRIGHT_MODULE", ""))
SCREENS = ROOT / "temp/automatic-reply-a4"


class Preview(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def proxy(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        upstream = http.client.HTTPConnection("127.0.0.1", self.server.api_port, timeout=5)
        try:
            upstream.request(self.command, self.path, data, dict(self.headers))
            response = upstream.getresponse()
            content = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "transfer-encoding", "content-length"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        finally:
            upstream.close()

    def do_GET(self):
        return self.proxy() if self.path.startswith("/api/") else super().do_GET()

    do_POST = proxy
    do_PUT = proxy


@pytest.mark.skipif(not PLAYWRIGHT.is_file(), reason="Set MILO_TEST_PLAYWRIGHT_MODULE explicitly")
def test_built_ui_follows_real_api_and_independent_service(service_case):
    preparation, services, start, _, _ = service_case
    database, owner, thread, _, directory, *_ = preparation
    web = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Preview, directory=str(ROOT / "frontend/dist"))
    )
    origin = f"http://127.0.0.1:{web.server_port}"
    app = create_app(Settings(database=database.path, origins=(origin,), secure_cookie=False))
    # Uvicorn binds an ephemeral socket before either server starts.
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", access_log=False)
    sock = config.bind_socket()
    web.api_port = sock.getsockname()[1]
    api = uvicorn.Server(config)
    api_thread = threading.Thread(target=api.run, kwargs={"sockets": [sock]}, daemon=True)
    web_thread = threading.Thread(target=web.serve_forever, daemon=True)
    command, response = directory / "browser-command.json", directory / "browser-response.json"
    metadata = directory / "browser.json"
    SCREENS.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata.write_text(
        json.dumps(
            {
                "synthetic": True,
                "origin": origin,
                "handle": "visitor",
                "password": PASSWORD,
                "thread": thread,
                "playwright": str(PLAYWRIGHT),
                "screens": str(SCREENS),
                "command": str(command),
                "response": str(response),
            }
        )
    )
    metadata.chmod(0o600)
    api_thread.start()
    web_thread.start()
    process = None
    try:
        wait_for(lambda: api.started)
        process = subprocess.Popen(
            ["node", str(Path(__file__).parent / "fixtures/reply_browser.mjs"), str(metadata)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        last = 0
        deadline = time.monotonic() + 35
        while process.poll() is None and time.monotonic() < deadline:
            if command.exists():
                request = json.loads(command.read_text())
                if request["id"] > last:
                    last = request["id"]
                    action = request["action"]
                    if action == "run":
                        EventPlanner(database).plan()
                        with database.connect(write=True) as connection:
                            connection.execute("UPDATE wake_events SET observed_at=observed_at-16")
                    elif action == "disable":
                        services.disable("reply", owner)
                    elif action == "fault":
                        services.fault("reply", owner, "execution_failed")
                    elif action == "resume":
                        services.resume("reply", owner)
                    elif action == "expire":
                        with database.connect(write=True) as connection:
                            connection.execute(
                                "UPDATE automatic_services SET checked_at=checked_at-76"
                            )
                    elif action != "check":
                        raise AssertionError("Unknown finite synthetic control")
                    if action != "expire":
                        start()
                    response.write_text(json.dumps({"id": last, "ok": True}))
            time.sleep(0.03)
        out, err = process.communicate(timeout=2)
        assert process.returncode == 0, err.decode()
        assert json.loads(out)["passed"] is True
        with database.snapshot() as connection:
            assert (
                connection.execute("SELECT count(*) FROM reply_execution_results").fetchone()[0]
                == 2
            )
        assert not list(directory.glob("*/credential.env"))
    finally:
        if process and process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
        api.should_exit = True
        web.shutdown()
        web.server_close()
        api_thread.join(timeout=3)
        web_thread.join(timeout=3)
        sock.close()

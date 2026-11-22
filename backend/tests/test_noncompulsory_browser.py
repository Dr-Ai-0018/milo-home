"""F6: built pages, loopback API, real consumers and synthetic native stdio only."""

import functools
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
from test_native_reply_execution import native_reply_case as native_reply_case
from test_native_reply_execution import tick
from test_reply_browser_entry import Preview
from test_reply_execution import ready
from test_reply_readiness import preparation as preparation
from test_work_process import fake_units as fake_units

from milo_home.app import create_app
from milo_home.automatic_services import AutomaticServices
from milo_home.codex_config import NewawzConfig
from milo_home.config import Settings
from milo_home.scheduler_cli import StopRequest
from milo_home.work_process import WorkLauncher
from milo_home.work_receiver import receive_once
from milo_home.work_runtime import WorkRuntime

ROOT = Path(__file__).parents[2]
PLAYWRIGHT = Path(os.environ.get("MILO_TEST_PLAYWRIGHT_MODULE", ""))


@pytest.mark.skipif(not PLAYWRIGHT.is_file(), reason="Explicit installed Playwright required")
@pytest.mark.parametrize("area", ["reply", "work"])
def test_combined_pages_and_consumers(native_reply_case, area):
    case, journal, _, scenario, _ = native_reply_case
    database, owner, thread, _, directory, profile, _ = case[0]

    def clock():
        return case[1][0]

    session = journal.business_binding().session_id
    services = AutomaticServices(database, clock)
    services.configure("reply", owner, "milo-home-reply.timer", enabled=True, revisits=True)
    services.configure(
        "work", owner, "milo-home-work.timer", session=session, enabled=True, work_decisions=True
    )
    for kind in ("reply", "work"):
        services.observe(kind, owner, timer_active=True)
    launcher = WorkLauncher(
        WorkRuntime(database, journal, clock), NewawzConfig.load(profile), journal.installation()
    )
    web = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Preview, directory=str(ROOT / "frontend/dist"))
    )
    origin = f"http://127.0.0.1:{web.server_port}"
    app = create_app(Settings(database=database.path, origins=(origin,), secure_cookie=False))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", access_log=False)
    sock = config.bind_socket()
    web.api_port = sock.getsockname()[1]
    api = uvicorn.Server(config)
    api_thread = threading.Thread(target=api.run, kwargs={"sockets": [sock]}, daemon=True)
    web_thread = threading.Thread(target=web.serve_forever, daemon=True)
    command, response = directory / "browser-command.json", directory / "browser-response.json"
    screens = ROOT / "temp/noncompulsory-f6"
    screens.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = directory / "browser.json"
    metadata.write_text(
        json.dumps(
            {
                "synthetic": True,
                "area": area,
                "origin": origin,
                "handle": "visitor",
                "password": PASSWORD,
                "thread": thread,
                "session": session,
                "now": clock(),
                "playwright": str(PLAYWRIGHT),
                "screens": str(screens),
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
        while not api.started:
            time.sleep(0.01)
        # HTTP services must share the synthetic consumer clock, including
        # default callables captured before monkeypatching time.time.
        for name in (
            "messages",
            "conversations",
            "activity",
            "schedules",
            "work_tasks",
            "work_access",
            "runs",
            "knocks",
        ):
            service = getattr(app.state, name)
            if hasattr(service, "clock"):
                service.clock = clock
        process = subprocess.Popen(
            [
                "node",
                str(Path(__file__).parent / "fixtures/noncompulsory_browser.mjs"),
                str(metadata),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        last = 0
        deadline = time.monotonic() + 36
        while process.poll() is None and time.monotonic() < deadline:
            if command.exists():
                request = json.loads(command.read_text())
                if request["id"] > last:
                    last = request["id"]
                    action = request["action"]
                    if action == "reply":
                        scenario[0] = request["scenario"]
                        ready(case)
                        result = tick(native_reply_case)
                        assert result["outcome"] == "succeeded", result
                    elif action == "return":
                        with database.snapshot() as connection:
                            case[1][0] = connection.execute(
                                "SELECT due_at FROM revisit_intents WHERE state='scheduled'"
                            ).fetchone()[0]
                        scenario[0] = "auto-reply-followup"
                        assert tick(native_reply_case)["outcome"] == "succeeded"
                    elif action == "work":
                        scenario[0] = request["scenario"]
                        _, result = launcher.run_task(
                            owner, request["task"], 60, credential="synthetic-private-file-key"
                        )
                        assert result.outcome == "completed"
                    elif action == "work-return":
                        case[1][0] += 61
                        scenario[0] = "auto-work-accepted"
                        result = receive_once(
                            launcher,
                            StopRequest(),
                            owner,
                            session,
                            60,
                            credential="synthetic-private-file-key",
                        )
                        assert result.outcome == "completed", result
                    else:
                        raise AssertionError("Unexpected finite synthetic browser control")
                    for kind in ("reply", "work"):
                        services.observe(kind, owner, timer_active=True)
                    response.write_text(json.dumps({"id": last, "ok": True, "now": clock()}))
            time.sleep(0.025)
        out, err = process.communicate(timeout=2)
        assert process.returncode == 0, err.decode()
        assert json.loads(out)["passed"] is True
        assert not journal.outstanding()
        assert not list(journal.path.parent.glob("*.env"))
        with database.snapshot() as connection:
            if area == "reply":
                assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 3
                assert connection.execute("SELECT count(*) FROM work_tasks").fetchone()[0] == 0
            else:
                assert connection.execute("SELECT count(*) FROM work_tasks").fetchone()[0] == 4
                assert (
                    connection.execute("SELECT count(*) FROM work_revisit_claims").fetchone()[0]
                    == 1
                )
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

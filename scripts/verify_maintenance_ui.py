"""Isolated real HTTP and production UI; no production data or model execution."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from milo_home.app import create_app
from milo_home.auth import AuthService
from milo_home.config import MaintenanceSettings, Settings
from milo_home.database import Database
from milo_home.database_identity import private_run_directory
from milo_home.maintenance_cache import publish_summary
from milo_home.maintenance_models import (
    BackupFact,
    CapacityFact,
    MaintenanceDocument,
    MaintenanceView,
)
from milo_home.work_access import WorkAccess
from starlette.staticfiles import StaticFiles


def prepare(root, origin):
    database = Database(root / "home.sqlite3")
    database.initialize()
    settings = Settings(database.path, (origin,), secure_cookie=False)
    auth = AuthService(database, settings)
    users = []
    for handle in ("synthetic-owner", "synthetic-other"):
        user, cookie = auth.activate(
            auth.create_invitation(handle), secrets.token_urlsafe(24), "合成维护验收"
        )
        users.append({"id": user["id"], "cookie": cookie})
    workspace = root / "workspace"
    workspace.mkdir()
    access = WorkAccess(database)
    access.register_workspace(str(uuid4()), "合成工作桌", workspace)
    access.grant(users[0]["id"])
    published = root / "published"
    published.mkdir(mode=0o750)
    selected = MaintenanceSettings(published / "summary.json", UUID(users[0]["id"]))
    settings = Settings(
        database.path, (origin,), secure_cookie=False, maintenance=selected
    )
    now = int(time.time())
    summary = MaintenanceView(
        observed_at=now,
        expires_at=now + 300,
        capacity=[
            CapacityFact(
                kind="business", state="normal", used_bytes=4096, complete=True
            )
        ],
        backups=[
            BackupFact(
                kind=kind,
                state="normal" if kind != "remote" else "unknown",
                last_outcome="succeeded" if kind != "remote" else "unknown",
                last_attempt_at=now - 100 if kind != "remote" else None,
                last_success_at=now - 90 if kind != "remote" else None,
                fresh_until=now + 600 if kind != "remote" else None,
            )
            for kind in ("daily", "paired", "remote", "restoration")
        ],
    )
    identity = database.path.stat()
    publish_summary(
        selected.file,
        MaintenanceDocument(
            owner_id=selected.owner,
            database_device=identity.st_dev,
            database_inode=identity.st_ino,
            summary=summary,
        ),
    )
    (root / "login.private.json").write_text(
        json.dumps({"synthetic": True, "users": users})
    )
    return create_app(settings)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case", choices=("core", "isolation"), required=True)
    parser.add_argument("--playwright", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.out.exists():
        raise ValueError("Select a new private output directory")
    private_run_directory(args.out.parent)
    args.out.mkdir(mode=0o700)
    root = private_run_directory(args.out)
    repository = Path(__file__).resolve().parents[1]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    app = prepare(root, origin)
    app.mount("/", StaticFiles(directory=repository / "frontend/dist", html=True))
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error", access_log=False
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("Isolated HTTP server did not start")
        subprocess.run(
            [
                "node",
                str(repository / "frontend/scripts/verify-maintenance-summary.mjs"),
                origin,
                str(root),
                args.case,
                str(args.playwright),
            ],
            check=True,
            timeout=35,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("Isolated HTTP server did not stop")
    print(json.dumps({"case": args.case, "evidence": str(root), "model_calls": 0}))


if __name__ == "__main__":
    main()

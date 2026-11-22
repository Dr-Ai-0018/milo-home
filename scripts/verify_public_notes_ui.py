"""Production bundle against isolated real HTTP and local content commands."""

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

import uvicorn
from milo_home.app import create_app
from milo_home.config import Settings
from milo_home.database import Database
from milo_home.database_identity import private_run_directory
from milo_home.public_notes import PublicNotes
from milo_home.public_notes_routes import router as public_notes_router
from starlette.staticfiles import StaticFiles


class FaultableNotes(PublicNotes):
    """Inject a storage failure at the service boundary, using the real error handler."""

    def __init__(self, database, root):
        super().__init__(database)
        self.root = root

    def check(self):
        if (self.root / "storage-busy").exists():
            error = sqlite3.OperationalError("synthetic storage busy")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error

    def list_notes(self, **kwargs):
        self.check()
        return super().list_notes(**kwargs)

    def get(self, note_id):
        self.check()
        return super().get(note_id)


def prepare(root, origin, mode):
    database = Database(root / "home.sqlite3")
    database.initialize()
    app = create_app(Settings(database.path, (origin,), secure_cookie=False))
    if mode == "legacy":
        app.router.routes[:] = [
            route
            for route in app.router.routes
            if getattr(route, "original_router", None) is not public_notes_router
        ]
    if mode == "history":
        notes = PublicNotes(database)
        for index in range(14):
            note_id = str(uuid4())
            notes.create(
                note_id,
                {
                    "title": f"合成历史 {index + 1:02d}",
                    "summary": "仅供独立验收的公开摘要，不是真实生活记录。" * 3,
                    "body": "合成历史正文。",
                    "category": None,
                    "content_date": "2026-11-22",
                },
            )
            notes.publish(note_id, 1)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--case", choices=("lifecycle", "history", "legacy"), required=True
    )
    parser.add_argument("--playwright", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    private_run_directory(args.out.parent)
    args.out.mkdir(mode=0o700)
    root = private_run_directory(args.out)
    repository = Path(__file__).resolve().parents[1]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    app = prepare(root, origin, args.case)
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
        app.state.public_notes = FaultableNotes(Database(root / "home.sqlite3"), root)
        subprocess.run(
            [
                "node",
                str(repository / "frontend/scripts/verify-public-notes.mjs"),
                origin,
                str(root),
                args.case,
                str(args.playwright),
            ],
            check=True,
            timeout=38,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("Isolated HTTP server did not stop")
    print(json.dumps({"case": args.case, "evidence": str(root), "model_calls": 0}))


if __name__ == "__main__":
    main()

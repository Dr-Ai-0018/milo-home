"""S5 production bundle against isolated real work APIs and explicit local publication."""

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from uuid import UUID

import uvicorn
from milo_home.app import create_app
from milo_home.config import PublicAssetSettings, Settings
from milo_home.database import Database
from milo_home.database_identity import private_run_directory
from milo_home.public_asset_store import PublicAssetStore
from milo_home.public_works import PublicWorks
from milo_home.public_works_routes import router as works_router
from starlette.staticfiles import StaticFiles


class FaultableWorks(PublicWorks):
    def __init__(self, database, root, store):
        super().__init__(database, store=store)
        self.root = root

    def check(self):
        if (self.root / "json-busy").exists():
            error = sqlite3.OperationalError("synthetic storage busy")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error

    def list_works(self, **options):
        self.check()
        return super().list_works(**options)

    def get(self, work_id):
        self.check()
        return super().get(work_id)


class FaultableAssets(PublicAssetStore):
    def __init__(self, root, controls):
        super().__init__(root, max_bytes=256 * 1024 * 1024)
        self.controls = controls

    def read(self, work_id, item):
        if (self.controls / "cover-busy").exists():
            raise OSError("Synthetic sealed asset failure")
        return super().read(work_id, item)


def prepare(root, origin, case):
    data, assets = root / "data", root / "public-assets"
    data.mkdir(mode=0o700)
    assets.mkdir(mode=0o700)
    database = Database(data / "home.sqlite3")
    database.initialize()
    app = create_app(
        Settings(
            database.path,
            (origin,),
            secure_cookie=False,
            public_assets=PublicAssetSettings(assets, 256 * 1024 * 1024),
        )
    )
    if case == "legacy":
        app.router.routes[:] = [
            route
            for route in app.router.routes
            if getattr(route, "original_router", None) is not works_router
        ]
    if case == "history":
        service = PublicWorks(database)
        for number in range(1, 15):
            identity = str(UUID(int=number))
            service.create(
                identity,
                {
                    "title": f"合成作品 {number:02d}",
                    "summary": "合成翻页核对，不是真实作品。",
                    "body": "仅供真实隔离HTTP联验。",
                    "content_date": "2026-11-22",
                    "links": [{"label": "访问示例", "url": "https://example.org/"}],
                },
            )
            service.publish(identity, 1)
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
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    app = prepare(root, origin, args.case)
    app.mount("/", StaticFiles(directory=repository / "frontend/dist", html=True))
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=int(origin.rsplit(":", 1)[1]),
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("Isolated HTTP did not start")
        app.state.public_works = FaultableWorks(
            app.state.database, root, FaultableAssets(root / "public-assets", root)
        )
        subprocess.run(
            [
                "node",
                str(repository / "frontend/scripts/verify-public-works.mjs"),
                origin,
                str(root),
                args.case,
                str(args.playwright),
            ],
            check=True,
            timeout=43,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("Isolated HTTP did not stop")
    print(json.dumps({"case": args.case, "evidence": str(root), "model_calls": 0}))


if __name__ == "__main__":
    main()

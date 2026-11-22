"""Private routing to execution evidence, written before any native process can start."""

import json
from pathlib import Path

from .codex_config import private_create, private_directory, private_read
from .codex_rpc import ProtocolError

ROUTE = "native-reply.json"
REQUIRED = "native-reply.required"


def has_native_route(base):
    return any((base / name).exists() or (base / name).is_symlink() for name in (ROUTE, REQUIRED))


def save_native_route(base, runtime, owner, run_id):
    private_directory(base.parent)
    base.mkdir(mode=0o700, exist_ok=False)
    private_create(base / REQUIRED, b"1\n")
    private_create(
        base / ROUTE,
        json.dumps(
            {
                "home": str(runtime.journal.path.parent),
                "fingerprint": runtime.journal.fingerprint(),
                "owner": owner,
                "run": run_id,
            },
            sort_keys=True,
        ).encode(),
    )


def read_native_route(base, owner, run_id):
    if not has_native_route(base):
        return None
    if private_read(base / REQUIRED) != b"1\n":
        raise ProtocolError("Native reply routing marker differs")
    route = json.loads(private_read(base / ROUTE))
    if (
        not isinstance(route, dict)
        or set(route) != {"home", "fingerprint", "owner", "run"}
        or route["owner"] != owner
        or route["run"] != run_id
        or not isinstance(route["home"], str)
        or not isinstance(route["fingerprint"], str)
    ):
        raise ProtocolError("Native reply recovery route differs from its run")
    private_directory(Path(route["home"]))
    return route

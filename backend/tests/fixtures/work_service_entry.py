"""Synthetic installation wrapper for real finite service tests; never contacts a provider."""

import json
import sys
import time
import traceback
from pathlib import Path

import milo_home.cli as cli
import milo_home.work_execution as execution
from milo_home.cli import main
from milo_home.codex_config import CodexRuntime, NewawzConfig
from milo_home.work_runtime import WorkRuntime

settings = json.loads(Path(sys.argv.pop(1)).read_text())
config = NewawzConfig(
    "synthetic-provider", "synthetic-model", "SYNTHETIC_KEY", settings["content"].encode()
)
NewawzConfig.load = staticmethod(lambda: config)
CodexRuntime.verify = lambda self: None
peer = Path(__file__).with_name("codex_app_server.py")
if settings["descendant"]:
    peer = Path(__file__).with_name("work_service_peer.py")
CodexRuntime.command = lambda self, *, workspace: [
    sys.executable,
    str(peer),
    settings["scenario"],
    settings["output"],
]
origin = time.monotonic()
time.time = lambda: settings["now"] + time.monotonic() - origin
execution.WorkRuntime = lambda database, journal: WorkRuntime(database, journal, time.time)
original_execute = cli.execute


def synthetic_diagnostic(args):
    try:
        return original_execute(args)
    except Exception:
        traceback.print_exc()
        raise


cli.execute = synthetic_diagnostic
main()

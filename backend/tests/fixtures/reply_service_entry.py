"""Replace only the provider executable; retain real service and isolation paths."""

import contextlib
import json
import sys
from pathlib import Path

import milo_home.worker as worker
from milo_home.cli import main

settings = json.loads(Path(sys.argv.pop(1)).read_text())
PROGRAM = r"""
import json, subprocess, sys, time
from pathlib import Path
snapshot = json.loads(sys.stdin.read().split("以下为本次唯一允许的业务输入：\n", 1)[1])
Path("/runtime/started").write_text("synthetic-executor")
if sys.argv[1] == "hang":
    child = subprocess.Popen(["/usr/bin/sleep", "30"])
    Path("/runtime/descendant").write_text(str(child.pid))
    time.sleep(30)
time.sleep(0.6)
if sys.argv[1] == "failed":
    print(json.dumps({"type": "turn.failed"}), flush=True)
    sys.exit(1)
result = {"replies": [{"message_id": item["id"], "body": "合成服务回信"}
                      for item in snapshot["selected"]]}
Path("/runtime/answer.json").write_text(json.dumps(result))
print(json.dumps({"type": "turn.completed"}), flush=True)
"""
worker.codex_arguments = lambda: ["/usr/bin/python3", "-c", PROGRAM, settings["mode"]]
with Path(settings["report"]).open("w") as report, contextlib.redirect_stdout(report):
    main()

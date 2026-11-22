"""Creation-only synthetic peer. Any attempted turn is a test failure."""

import json
import sys
from pathlib import Path

scenario, output = sys.argv[1:]
candidate = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

for line in sys.stdin:
    request = json.loads(line)
    with Path(output).open("a") as stream:
        stream.write(json.dumps(request) + "\n")
    method = request["method"]
    if method.startswith("turn/"):
        sys.exit(91)
    if "id" not in request:
        continue
    if method == "initialize":
        print(
            json.dumps(
                {"method": "remoteControl/status/changed", "params": {"status": "disabled"}}
            ),
            flush=True,
        )
    if (method, scenario) in {
        ("thread/start", "create-eof"),
        ("thread/inject_items", "marker-eof"),
        ("thread/resume", "resume-eof"),
    }:
        sys.exit(0)
    result = {}
    if method in {"thread/start", "thread/resume"}:
        thread = request["params"].get("threadId", candidate)
        result = {
            "thread": {"id": thread, "ephemeral": False, "status": {"type": "idle"}},
            "model": "synthetic-model",
            "modelProvider": "fallback" if scenario == "wrong-provider" else "synthetic-provider",
            "cwd": request["params"]["cwd"],
            "sandbox": {"type": "dangerFullAccess"},
            "approvalPolicy": "never",
        }
        if scenario == "bad-uuid":
            result["thread"]["id"] = "not-an-existing-thread"
        if method == "thread/resume" and scenario == "resume-active":
            result["thread"]["status"]["type"] = "active"
    print(json.dumps({"id": request["id"], "result": result}), flush=True)

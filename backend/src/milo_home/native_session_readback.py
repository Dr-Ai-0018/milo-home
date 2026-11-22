"""Finite metadata-only verification of a fresh native recovery copy."""

import time
from pathlib import Path

from .codex_process import DirectUnit
from .codex_rpc import ProtocolError, StdioRpc
from .native_session_backup import independent_output, rehearse_native, verified_manifest
from .native_session_relocation import relocate_rehearsal
from .workspace_archive_files import DEFAULT_BUDGET, complete, json_bytes, new_directory, write_file


def verify_history(rpc, chosen, workspace):
    rpc.request(
        "initialize",
        {
            "clientInfo": {"name": "milo_home_restore_check", "version": "0.1.0"},
            "capabilities": {"experimentalApi": False},
        },
    )
    rpc.notify_initialized()
    result = rpc.request("thread/read", {"threadId": chosen["thread_id"], "includeTurns": True})
    thread = result.get("thread", {})
    if not isinstance(thread, dict):
        raise ProtocolError("Native readback has malformed thread metadata")
    turns = thread.get("turns", [])
    if (
        thread.get("id") != chosen["thread_id"]
        or thread.get("path") != chosen["path"]
        or not isinstance(turns, list)
        or any(not isinstance(turn, dict) for turn in turns)
        or [turn.get("id") for turn in turns] != chosen["turn_ids"]
    ):
        raise ProtocolError("Native readback does not match the restored history")
    resumed = rpc.request(
        "thread/resume",
        {
            "threadId": chosen["thread_id"],
            "excludeTurns": True,
            "cwd": str(workspace),
            "sandbox": "danger-full-access",
            "approvalPolicy": "never",
            "modelProvider": chosen["provider"],
            "model": chosen["model"],
        },
    )
    if (
        resumed.get("thread", {}).get("id") != chosen["thread_id"]
        or resumed.get("thread", {}).get("path") != chosen["path"]
        or resumed.get("modelProvider") != chosen["provider"]
        or resumed.get("model") != chosen["model"]
        or resumed.get("cwd") != str(workspace)
        or resumed.get("sandbox", {}).get("type") != "dangerFullAccess"
        or resumed.get("approvalPolicy") != "never"
        or resumed.get("thread", {}).get("status", {}).get("type") != "idle"
    ):
        raise ProtocolError("Native resume does not match the explicit rehearsal configuration")
    return len(turns)


def readback_native(archive, output, digest, installed, *, seconds=25, budget=DEFAULT_BUDGET):
    if type(seconds) is not int or not 5 <= seconds <= 30:
        raise ValueError("Native readback requires a 5–30 second window")
    deadline = time.monotonic() + seconds
    manifest = verified_manifest(archive, digest, budget)
    independent_output(archive, output)
    independent_output(Path(manifest["source"]), output)
    installed.verify()
    required = sum(item["bytes"] for item in manifest["artifacts"].values()) * 4 + 4 * 1024**2
    new_directory(output, budget, required)
    home = output / "home"
    rehearse_native(archive, home, digest, budget=budget)
    chosen, config, mapped = relocate_rehearsal(home, manifest)
    workspace = output / "workspace"
    workspace.mkdir(mode=0o700)
    unit = DirectUnit(seconds=seconds)
    unit.deadline = min(unit.deadline, deadline)
    environment = output / "readback.env"
    write_file(environment, installed.environment(home, config, "synthetic-never-call-provider"))
    events = []
    rpc = None
    stopped = False
    try:
        while time.monotonic() < unit.next_cpu:
            unit.checkpoint()
            time.sleep(0.02)
        process = unit.launch(installed.command(), environment_file=environment)
        unit.wait_ready()
        rpc = StdioRpc(
            process,
            deadline=unit.deadline,
            record=events.append,
            stop=unit.stop,
            checkpoint=unit.checkpoint,
        )
        count = verify_history(rpc, chosen, workspace)
    finally:
        try:
            stopped = rpc.close() if rpc else (unit.stop() if unit.launch_attempted else True)
            if rpc is None and unit.process is not None:
                for pipe in (unit.process.stdin, unit.process.stdout, unit.process.stderr):
                    pipe.close()
            write_file(
                output / "protocol.json",
                json_bytes(
                    {
                        "events": events,
                        "stopped": stopped,
                        "unit": unit.name,
                        "invocation": unit.invocation,
                    }
                ),
            )
        finally:
            environment.unlink(missing_ok=True)
        if not stopped:
            raise ProtocolError("Native readback family stop remains unconfirmed")
    verified_manifest(archive, digest, budget)
    methods = [event["request"]["method"] for event in events if event["kind"] == "intent"]
    if methods != ["initialize", "thread/read", "thread/resume"]:
        raise ProtocolError("Native readback performed an unexpected request")
    report = {
        "protocol_restore_verified": True,
        "historical_turns": count,
        "relocated_index_rows": mapped,
        "original_archive_modified": False,
        "stop_confirmed": stopped,
        "model_calls": 0,
        "automatic_resume": False,
        "production_configuration_verified": False,
    }
    write_file(output / "report.json", json_bytes(report))
    complete(output)
    return report

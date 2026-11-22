"""Close only an uninitialized creation, retaining its unknown delivery evidence."""

import hashlib
import json
import re
import subprocess
import time
import tomllib
from pathlib import Path

from .backup_process_evidence import observe_family
from .codex_config import private_read
from .codex_rpc import ProtocolError
from .work_creation_records import ABANDONED, choice_digest, start_parameters
from .work_native_absence import verify_native_absence
from .work_private_files import sync_directory
from .work_runtime import WorkRuntime
from .work_stop import recorded_identity


def _original_events(records, choice):
    events = list(records.events)
    existing = events.pop() if events and events[-1].get("kind") == ABANDONED else None
    if len(events) < 7 or set(records.path.iterdir()) != {
        records.path / f"{i:06d}.json" for i in range(1, len(records.events) + 1)
    }:
        raise ProtocolError("Incomplete original creation event sequence")
    launch, ready, *rpc, stopped = events
    unit = ready.get("unit")
    identity = recorded_identity(enumerate(events), unit)
    if (
        any(e.get("connection") != unit for e in events)
        or launch != {"kind": "process", "phase": "launch_intent", "connection": unit}
        or ready != {"kind": "process", "phase": "ready", "connection": unit, **identity}
        or stopped != {"kind": "process", "phase": "stop_confirmed", "connection": unit}
    ):
        raise ProtocolError("Expected one original stopped creation family")
    requests = [
        e
        for e in rpc
        if not (
            e.get("kind") == "notification"
            and set(e) == {"kind", "connection", "message"}
            and isinstance(e["message"], dict)
            and e["message"].get("method") == "remoteControl/status/changed"
            and "id" not in e["message"]
        )
    ]
    if len(requests) != 4:
        raise ProtocolError("Expected only initialization and one unknown thread start")
    init, response, start, unknown = [
        {k: v for k, v in e.items() if k != "connection"} for e in requests
    ]
    reply = response.get("response")
    if (
        init
        != {
            "kind": "intent",
            "request": {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "milo_home", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": False},
                },
            },
        }
        or not isinstance(reply, dict)
        or response != {"kind": "response", "id": 1, "response": reply}
        or set(reply) != {"id", "result"}
        or reply["id"] != 1
        or not isinstance(reply["result"], dict)
        or start
        != {
            "kind": "intent",
            "request": {
                "id": 2,
                "method": "thread/start",
                "params": start_parameters(choice),
            },
        }
        or unknown != {"kind": "unknown", "id": 2}
    ):
        raise ProtocolError("Original initialization or unknown thread start differs")
    original = hashlib.sha256()
    for i in range(1, len(events) + 1):
        data = private_read(records.path / f"{i:06d}.json", limit=512 * 1024)
        if json.loads(data) != events[i - 1]:
            raise ProtocolError("Original events changed while being verified")
        original.update(len(data).to_bytes(4, "big"))
        original.update(data)
    return identity, original.hexdigest(), existing


def _configuration(claim):
    content = private_read(claim.directory / "config.toml")
    digest = hashlib.sha256(content).hexdigest()
    original = content
    if digest != claim.choice["config_digest"]:
        # The observed writeback appended one table. Match the byte-exact
        # original prefix; don't read the current profile or trust a rewrite.
        sections = list(re.finditer(rb"(?m)^\[projects\.", content))
        if len(sections) != 1:
            raise ProtocolError("Original creation configuration differs")
        original = content[: sections[0].start()].rstrip(b"\n") + b"\n"
        if hashlib.sha256(original).hexdigest() != claim.choice["config_digest"]:
            raise ProtocolError("Original creation configuration differs")
        actual = tomllib.loads(content.decode())
        expected = tomllib.loads(original.decode())
        if "projects" in expected:
            raise ProtocolError("Unexpected original project trust settings")
        expected["projects"] = {claim.choice["cwd"]: {"trust_level": "trusted"}}
        if actual != expected:
            raise ProtocolError("Only the recorded workspace trust writeback is allowed")
    base = tomllib.loads(original.decode())
    if (base.get("model_provider"), base.get("model")) != (
        claim.choice["provider"],
        claim.choice["model"],
    ):
        raise ProtocolError("Original creation provider or model differs")
    return digest


def _check_transaction(claim, connection):
    claim._check_transaction(connection)
    row = connection.execute(
        "SELECT * FROM work_main_creations WHERE id=?", (claim.choice["id"],)
    ).fetchone()
    if (
        row["state"] != "unknown"
        or any(
            row[key] != claim.choice[key]
            for key in ("owner_revision", "workspace_revision", "created_at", "deadline_at")
        )
        or connection.execute(
            "SELECT 1 FROM work_runtime_bindings WHERE session_id=? "
            "UNION ALL SELECT 1 FROM work_tasks WHERE session_id=? LIMIT 1",
            (claim.session_id, claim.session_id),
        ).fetchone()
    ):
        raise ProtocolError("Original unknown creation is no longer eligible for abandonment")


def abandon(claim):
    claim.load_original()
    claim.verify_attempt_history()
    claim.access = tuple(claim.choice[k] for k in ("owner_revision", "workspace_revision"))
    root = Path(claim.choice["cwd"])
    if root.resolve() != root or not root.is_dir():
        raise ProtocolError("Original workspace is no longer canonical")
    with claim.database.connect() as connection:
        connection.execute("BEGIN")
        _check_transaction(claim, connection)
    identity, prefix_digest, existing = _original_events(claim.records, claim.choice)

    def observe_command(command, *, timeout, **options):
        return subprocess.run(command, timeout=min(timeout, claim.limit.remaining()), **options)

    if claim.stop.is_set() or not observe_family(identity, run=observe_command)["absent_observed"]:
        raise ProtocolError("Original creation stop needs reconciliation")
    config_digest = _configuration(claim)
    native_digest = verify_native_absence(claim.directory)
    audit = {
        "kind": ABANDONED,
        "connection": identity["unit"],
        "choice_digest": choice_digest(claim.choice),
        "original_events_digest": prefix_digest,
        "observed_config_digest": config_digest,
        "native_evidence_digest": native_digest,
        "stopped_family": identity,
        "thread_start_delivery": "unknown",
    }
    claim.limit.remaining()
    if claim.stop.is_set():
        raise ProtocolError("Main creation abandonment stop requested")
    if existing is None:
        claim.records.record({**audit, "checked_at": int(time.time())})
    else:
        checked_at = existing.get("checked_at")
        if (
            type(checked_at) is not int
            or checked_at < 0
            or existing != {**audit, "checked_at": checked_at}
        ):
            raise ProtocolError("Existing abandonment evidence differs")
        sync_directory(claim.records.path)
    WorkRuntime(claim.database, None)._identity(claim.lease)
    with claim.database.connect(write=True) as connection:
        _check_transaction(claim, connection)
        claim.limit.remaining()
        if claim.stop.is_set():
            raise ProtocolError("Main creation abandonment stop requested")
        connection.execute(
            "UPDATE work_main_creations SET state='closed' WHERE id=? AND state='unknown'",
            (claim.choice["id"],),
        )
    return {
        "creation_abandoned": True,
        "thread_start_delivery": "unknown",
        "execution_occupancy_released": True,
        "association_recorded": False,
        "execution_available": False,
    }

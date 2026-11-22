"""Immutable bounded creation evidence, independent of a bound task journal."""

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from .codex_app_server import ThreadBinding
from .codex_config import private_directory, private_read
from .codex_rpc import ProtocolError
from .work_private_files import publish_file, sync_directory

CHOICE = "main-creation.json"
EVENTS = "main-creation-events"
HISTORY = "main-creation-history"
MARKER = "米洛小家专用主会话初始化记录；此时尚未收到工作任务，不启动模型工作。"
ABANDONED = "abandoned_before_initialization"


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n").encode()


def choice_digest(choice):
    return hashlib.sha256(encoded(choice)).hexdigest()


def read_choice(directory):
    value = json.loads(private_read(directory / CHOICE))
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ProtocolError("Invalid main creation choice")
    return value


class CreationRecords:
    def __init__(self, directory, *, create=False):
        self.path = directory / EVENTS
        if create:
            self.path.mkdir(mode=0o700)
            sync_directory(directory)
        private_directory(self.path)
        paths = sorted(self.path.glob("*.json"))
        if len(paths) > 128:
            raise ProtocolError("Creation evidence limit")
        self.events = []
        self.size = 0
        for number, path in enumerate(paths, 1):
            if path.name != f"{number:06d}.json":
                raise ProtocolError("Creation evidence sequence changed")
            data = private_read(path, limit=512 * 1024)
            event = json.loads(data)
            if not isinstance(event, dict):
                raise ProtocolError("Invalid creation evidence")
            self.size += len(data)
            self.events.append(event)
        if self.size > 8 * 1024 * 1024:
            raise ProtocolError("Creation evidence limit")

    def record(self, event):
        data = encoded(event)
        if (
            len(self.events) >= 128
            or len(data) > 512 * 1024
            or self.size + len(data) > 8 * 1024 * 1024
        ):
            raise ProtocolError("Creation evidence limit")
        publish_file(self.path / f"{len(self.events) + 1:06d}.json", data)
        self.events.append(event)
        self.size += len(data)


def start_parameters(choice):
    return {
        "cwd": choice["cwd"],
        "sandbox": "danger-full-access",
        "approvalPolicy": "never",
        "ephemeral": False,
    }


def marker_parameters(thread):
    return {
        "threadId": thread,
        "items": [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": MARKER}],
            }
        ],
    }


def response_for(records, intent):
    request = intent["request"]
    matching = [
        e
        for e in records.events
        if e.get("connection") == intent["connection"] and e.get("id") == request["id"]
    ]
    responses = [e for e in matching if e.get("kind") == "response"]
    if len(responses) != 1 or any(e.get("kind") == "unknown" for e in matching):
        raise ProtocolError("Original creation response needs reconciliation")
    response = responses[0]["response"]
    if (
        response.get("id") != request["id"]
        or "error" in response
        or not isinstance(response.get("result"), dict)
    ):
        raise ProtocolError("Original creation request did not succeed")
    return response["result"]


def verify_result(result, choice, *, thread=None):
    try:
        candidate = result["thread"]["id"]
        valid = (
            str(UUID(candidate)) == candidate
            and (thread is None or candidate == thread)
            and result["thread"]["ephemeral"] is False
            and result["thread"]["status"]["type"] == "idle"
            and result["modelProvider"] == choice["provider"]
            and result["model"] == choice["model"]
            and result["cwd"] == choice["cwd"]
            and result["sandbox"]["type"] == "dangerFullAccess"
            and result["approvalPolicy"] == "never"
        )
        if not valid:
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProtocolError("Created main effective configuration mismatch") from None
    return ThreadBinding(candidate, choice["provider"], choice["model"], Path(choice["cwd"]))


def original_binding(records, choice):
    ensure_not_abandoned(records)
    starts = [
        e
        for e in records.events
        if e.get("kind") == "intent" and e.get("request", {}).get("method") == "thread/start"
    ]
    if len(starts) != 1 or starts[0]["request"]["params"] != start_parameters(choice):
        raise ProtocolError("Original unique creation request is missing")
    binding = verify_result(response_for(records, starts[0]), choice)
    markers = [
        e
        for e in records.events
        if e.get("kind") == "intent" and e.get("request", {}).get("method") == "thread/inject_items"
    ]
    if (
        len(markers) != 1
        or markers[0]["connection"] != starts[0]["connection"]
        or markers[0]["request"]["params"] != marker_parameters(binding.thread_id)
    ):
        raise ProtocolError("Original initialization marker is missing")
    response_for(records, markers[0])
    return binding


def ensure_not_abandoned(records):
    if any(event.get("kind") == ABANDONED for event in records.events):
        raise ProtocolError("Original main creation was abandoned before initialization")


def resume_verified(records, choice, binding):
    requests = [
        e
        for e in records.events
        if e.get("kind") == "intent" and e.get("request", {}).get("method") == "thread/resume"
    ]
    for intent in reversed(requests):
        try:
            result = response_for(records, intent)
        except ProtocolError:
            # A read-only resume can be retried explicitly after freshly
            # stopping its original family. Creation/injection cannot.
            continue
        verify_result(result, choice, thread=binding.thread_id)
        if intent["request"]["params"].get("threadId") != binding.thread_id:
            raise ProtocolError("Creation resume identity changed")
        return True
    return False


def process_states(records):
    states = {}
    for event in records.events:
        if event.get("kind") == "process":
            states[event["connection"]] = event["phase"]
    return states


def guard_registration(database, directory, binding, owner, session_id, lease, installed):
    from .work_access import require_access
    from .work_attempt_history import attempt_rows, read_history

    choice_path = directory / CHOICE
    if not choice_path.exists() and not choice_path.is_symlink():
        if any(
            (directory / name).exists() or (directory / name).is_symlink()
            for name in (EVENTS, HISTORY)
        ):
            raise ProtocolError("Main creation choice is missing")
        with database.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM work_main_creations WHERE session_id=?", (session_id,)
            ).fetchone():
                raise ProtocolError("Original main creation home is required")
        return
    choice = read_choice(directory)
    records = CreationRecords(directory)
    original = original_binding(records, choice)
    metadata = os.fstat(lease.descriptor)
    if (
        original != binding
        or choice["codex_home"] != str(directory)
        or choice["cli"] != str(installed.cli)
        or choice["tool_host"] != str(installed.tool_host)
        or choice["config_digest"]
        != hashlib.sha256(private_read(directory / "config.toml")).hexdigest()
        or choice["owner_id"] != owner
        or choice["session_id"] != session_id
        or choice["database"] != str(lease.canonical)
        or choice["database_owner"] != lease.owner
        or (choice["database_device"], choice["database_inode"])
        != (metadata.st_dev, metadata.st_ino)
        or not resume_verified(records, choice, original)
        or not process_states(records)
        or set(process_states(records).values()) != {"stop_confirmed"}
    ):
        raise ProtocolError("Main creation still needs reconciliation")
    with database.connect() as connection:
        connection.execute("BEGIN")
        attempts, retirements, grants = attempt_rows(connection, session_id)
        successor_access = None
        if choice.get("supersedes_id") is not None:
            successor_access = choice["owner_revision"], choice["workspace_revision"]
            if tuple(require_access(connection, owner, choice["workspace_id"])) != successor_access:
                raise ProtocolError("Successor authorization revision changed")
    if not any(row["id"] == choice["id"] and row["state"] == "closed" for row in attempts):
        raise ProtocolError("Original business creation is not closed")
    read_history(directory, choice, attempts, retirements, grants)
    return successor_access

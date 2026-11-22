"""Match original website reply runs to native ledgers without executing or rebinding."""

import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .native_messages import NativeMessageStore
from .native_reply_input import native_input_digest
from .native_session_files import sqlite_snapshot
from .runtime_evidence_backup import runtime_inventory


def reply_business_state(connection):
    runs = [
        dict(row)
        for row in connection.execute(
            "SELECT r.id,r.thread_id,r.conversation_id,r.unit_name,r.status,s.owner_id,"
            "t.owner_id AS thread_owner_id FROM runs r JOIN reply_run_scopes s ON s.run_id=r.id "
            "JOIN threads t ON t.id=r.thread_id ORDER BY r.id"
        )
    ]
    inputs = [
        dict(row)
        for row in connection.execute(
            "SELECT m.run_id,m.message_id FROM run_messages m "
            "JOIN reply_run_scopes s ON s.run_id=m.run_id ORDER BY m.run_id,m.message_id"
        )
    ]
    return {"runs": runs, "inputs": inputs}


def check_reply_association(business, journal, streams, routes, budget):
    """Missing later facts may be a crash; conflicting recorded facts are never accepted."""
    projected = business.get("native_replies")
    if projected is None:
        if streams:
            raise ValueError("Native reply streams require their paired recovery routes")
        return 0
    if routes is None:
        raise ValueError("Native reply recovery routes are required")
    rows = {row["id"]: row for row in projected["runs"]}
    covered = set(routes["routes"])
    linked = journal.business_binding()
    for run_id in covered:
        row = rows.get(run_id)
        if (
            row is None
            or row["owner_id"] != linked.owner_id
            or row["thread_owner_id"] != linked.owner_id
            or row["unit_name"] != "milo-reply-" + run_id
            or routes["routes"][run_id]["owner"] != linked.owner_id
        ):
            raise ValueError("Native reply route belongs to another owner or execution family")
    stream_by_run = {}
    for stream in streams:
        unit = stream["unit_name"]
        run_id = next(
            (identity for identity in covered if unit == "milo-reply-" + identity + ".service"),
            None,
        )
        if run_id is None or run_id in stream_by_run or rows[run_id]["owner_id"] != linked.owner_id:
            raise ValueError("Native reply stream differs from its original run")
        stream_by_run[run_id] = stream["stream_id"]
    scopes = _message_scopes(journal, budget)
    for scope, events in scopes:
        run_id = str(scope.run_id)
        row = rows.get(run_id)
        expected = {item["message_id"] for item in projected["inputs"] if item["run_id"] == run_id}
        if (
            run_id not in covered
            or run_id not in stream_by_run
            or row is None
            or row["owner_id"] != str(scope.owner_id)
            or row["conversation_id"] != str(scope.conversation_id)
            or expected != {str(identity) for identity in scope.message_ids}
            or any(event.stream_id != stream_by_run[run_id] for event in events)
        ):
            raise ValueError("Native reply message mapping differs from its business scope")
    by_run = {str(scope.run_id): scope for scope, _ in scopes}
    sent_by_run = {str(scope.run_id): events[0] for scope, events in scopes if events}
    for run_id, stream in stream_by_run.items():
        for _, event in journal.stream_snapshot(stream)["entries"]:
            if (
                event.get("kind") == "process"
                and event.get("phase") == "launch_intent"
                and event.get("reply_run_id") != run_id
            ):
                raise ValueError("Native process launch belongs to another reply run")
            if (
                event.get("kind") == "intent"
                and event.get("request", {}).get("method") == "turn/start"
            ):
                scope = by_run.get(run_id)
                if scope is None or event.get("reply") != scope.model_dump(mode="json"):
                    raise ValueError("Native turn intent differs from its message mapping")
                request = event["request"]
                params = request.get("params", {})
                inputs = params.get("input")
                sent = sent_by_run.get(run_id)
                if (
                    sent is None
                    or request.get("id") != sent.request_id
                    or params.get("threadId") != scope.native_thread_id
                    or params.get("clientUserMessageId") != scope.input_id
                    or not isinstance(inputs, list)
                    or len(inputs) != 1
                    or not isinstance(inputs[0], dict)
                    or inputs[0].get("type") != "text"
                    or not isinstance(inputs[0].get("text"), str)
                    or native_input_digest(inputs[0]["text"]) != scope.input_digest
                ):
                    raise ValueError("Native turn request conflicts with its saved input")
    return sum(
        row["status"] in {"queued", "running", "stopping"}
        for identity, row in rows.items()
        if identity in covered
    )


def _message_scopes(journal, budget):
    home = journal.path.parent
    inventory = runtime_inventory(home, budget)
    name = "native-message-links.sqlite3"
    if name not in inventory["files"]:
        if "native-message-links.required" in inventory["files"]:
            raise ValueError("Native message ledger is missing")
        return []
    with tempfile.TemporaryDirectory(prefix="milo-paired-replies-") as temporary:
        path = Path(temporary) / name
        sqlite_snapshot(home / name, path, budget, inventory["files"])
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return [
                NativeMessageStore(journal)._read(db, row[0])
                for row in db.execute("SELECT run_id FROM deliveries ORDER BY run_id")
            ]

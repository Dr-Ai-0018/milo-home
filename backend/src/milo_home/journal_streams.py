"""Consistent stream snapshots and atomic fact-only repair storage."""

import json
import re

from . import journal_segments, journal_storage
from .codex_rpc import ProtocolError
from .journal_capacity import REPAIR_LIMIT, TERMINAL_RESERVE, check_append, encode
from .work_receipts import receipt_shape, validated_receipts
from .work_stop import stop_shape


def snapshot(connection, stream):
    row = connection.execute("SELECT * FROM streams WHERE id=?", (stream,)).fetchone()
    if row is None:
        raise ProtocolError("Private journal stream is missing")
    work = connection.execute("SELECT * FROM stream_work WHERE stream=?", (stream,)).fetchone()
    return {
        "stream": dict(row),
        "work": dict(work) if work else None,
        "entries": [
            (sequence, json.loads(payload))
            for sequence, payload in journal_segments.raw_entries(connection, stream)
        ],
        "requests": [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM requests WHERE stream=? ORDER BY request_id", (stream,)
            )
        ],
    }


def list_streams(connection, *, unit=None, limit=None):
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("Positive stream read bound is required")
    rows = connection.execute(
        "SELECT * FROM streams "
        + ("WHERE unit=? " if unit is not None else "")
        + "ORDER BY id"
        + (" LIMIT ?" if limit is not None else ""),
        ((unit,) if unit is not None else ()) + ((limit,) if limit is not None else ()),
    )
    return [
        {
            "stream": dict(row),
            "work": dict(work) if work else None,
        }
        for row in rows
        for work in [
            connection.execute("SELECT * FROM stream_work WHERE stream=?", (row["id"],)).fetchone()
        ]
    ]


def process_events(connection, stream, *, limit):
    if type(limit) is not int or limit < 1:
        raise ValueError("Positive process read bound is required")
    if journal_storage.version(connection) == 3:
        events = (
            json.loads(payload) for _, payload in journal_segments.raw_entries(connection, stream)
        )
        from itertools import islice

        return list(islice((event for event in events if event.get("kind") == "process"), limit))
    return [
        json.loads(row[0])
        for row in connection.execute(
            "SELECT payload FROM entries WHERE stream=? "
            "AND json_extract(payload,'$.kind')='process' ORDER BY sequence LIMIT ?",
            (stream, limit),
        )
    ]


def validate_process(stream, event):
    if event.get("unit") != stream["unit"] or event.get("phase") not in {
        "launch_intent",
        "ready",
        "stop_confirmed",
        "stop_unknown",
    }:
        raise ProtocolError("Invalid process evidence")
    if event["phase"] == "ready" and (
        not isinstance(event.get("invocation"), str)
        or re.fullmatch(r"[0-9a-f]{32}", event["invocation"]) is None
        or type(event.get("main_pid")) is not int
        or event["main_pid"] <= 0
    ):
        raise ProtocolError("Missing process identity")


def append_repair(connection, expected, receipts, marker, proof):
    stream = expected["stream"]["id"]
    current = snapshot(connection, stream)
    if current != expected:
        raise ProtocolError("Private evidence changed during repair")
    stop_shape(proof)
    if proof["unit"] != current["stream"]["unit"] or any(
        request["state"] != "acknowledged" for request in current["requests"]
    ):
        raise ProtocolError("Private requests changed during repair")
    if not receipts or len(receipts) > 100 or marker.get("kind") != "work_receipt_repair":
        raise ProtocolError("Invalid private receipt repair batch")
    for receipt in receipts:
        receipt_shape(receipt)
        if "output" in receipt:
            raise ProtocolError("Receipt repair cannot manufacture output")
    validate_repair(connection, current, receipts, marker, proof)
    batch = [encode(event) for event in [*receipts, marker, proof]]
    size = sum(len(payload.encode()) for payload in batch)
    if size > REPAIR_LIMIT:
        raise ProtocolError("Private journal repair batch limit")
    check_append(connection, current["stream"], size, reserve=TERMINAL_RESERVE)
    for payload in batch:
        sequence = journal_storage.insert_entry(connection, stream, payload)
    connection.execute("UPDATE streams SET bytes=bytes+? WHERE id=?", (size, stream))
    return sequence


def validate_repair(connection, current, receipts, marker, proof):
    work = current["work"]
    if work is None:
        raise ProtocolError("Receipt repair requires the original work binding")
    row = connection.execute("SELECT value FROM binding WHERE singleton=1").fetchone()
    thread = json.loads(row[0])["thread_id"]
    entries = current["entries"]
    last = entries[-1][0] if entries else 0
    validated_receipts(
        entries + [(last + index + 1, receipt) for index, receipt in enumerate(receipts)],
        stream_id=current["stream"]["id"],
        window_id=work["window_id"],
        task_id=work["task_id"],
        thread_id=thread,
    )
    sources = []
    for receipt in receipts:
        request_id = receipt["request_id"]
        intents = [
            s for s, e in entries if e.get("kind") == "intent" and e["request"]["id"] == request_id
        ]
        responses = [s for s, e in entries if e.get("kind") == "response" and e["id"] == request_id]
        if len(intents) != 1 or len(responses) != 1 or responses[0] <= intents[0]:
            raise ProtocolError("Repair provenance lacks unique original request evidence")
        terminals = [
            s
            for s, e in entries
            if e.get("kind") == "notification"
            and e["message"].get("method") == "turn/completed"
            and e["message"].get("params", {}).get("threadId") == thread
            and e["message"].get("params", {}).get("turn", {}).get("id") == receipt["turn_id"]
        ]
        if any(s <= intents[0] for s in terminals):
            raise ProtocolError("Repair terminal predates its original request")
        sources.append(
            {
                "input_id": receipt["work"]["input_id"],
                "request_sequence": intents[0],
                "response_sequence": responses[0],
                "terminal_sequences": terminals,
            }
        )
    if (
        set(marker) != {"kind", "source", "checked_at", "evidence"}
        or marker["source"] not in {"original_protocol", "original_protocol_and_native_rollout"}
        or marker["checked_at"] != proof["checked_at"]
        or marker["evidence"] != sources
        or any(receipt["observed_at"] != proof["checked_at"] for receipt in receipts)
    ):
        raise ProtocolError("Repair provenance differs from original evidence")

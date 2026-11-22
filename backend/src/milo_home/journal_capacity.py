"""Bounded journal admission and space reserved for one failure cleanup."""

import json

from . import journal_storage
from .codex_rpc import ProtocolError

MIB = 1024 * 1024
STREAM_LIMIT = 8 * MIB
TOTAL_LIMIT = 32 * MIB
ENTRY_LIMIT = MIB // 2
ORDINARY_RESERVE = 3 * MIB // 2
RESPONSE_RESERVE = MIB
TERMINAL_RESERVE = MIB // 4
SMALL_EVENT_LIMIT = 4096
REPAIR_LIMIT = 3 * MIB // 4 - 16 * 1024


def encode(event):
    payload = json.dumps(event, ensure_ascii=False, allow_nan=False)
    if len(payload.encode()) > ENTRY_LIMIT:
        raise ProtocolError("Private journal entry limit")
    return payload


def total_bytes(connection):
    logical = connection.execute("SELECT COALESCE(SUM(bytes),0) FROM streams").fetchone()[0]
    if journal_storage.version(connection) == 3:
        logical -= connection.execute(
            "SELECT coalesce(sum(payload_bytes),0) FROM stream_archives"
        ).fetchone()[0]
        if logical < 0:
            raise ProtocolError("Journal hot capacity differs from its immutable segments")
    return logical


def capacity(connection):
    used = total_bytes(connection)
    result = {
        "used_bytes": used,
        "limit_bytes": TOTAL_LIMIT,
        "cleanup_reserve_bytes": ORDINARY_RESERVE,
        "new_stream_budget_bytes": STREAM_LIMIT,
        "can_open_stream": used + STREAM_LIMIT <= TOTAL_LIMIT,
    }
    if journal_storage.version(connection) == 3:
        from .journal_segments import inventory

        records = inventory(connection)
        result["archived_bytes"] = sum(record["file_bytes"] for record in records)
        result["archived_streams"] = len(records)
    return result


def can_admit(report):
    """A fresh native round needs both hot headroom and unexhausted cold storage."""
    from .journal_segments import MAX_SEGMENTS, STORAGE_LIMIT

    return (
        report["can_open_stream"]
        and report.get("archived_bytes", 0) < STORAGE_LIMIT
        and report.get("archived_streams", 0) < MAX_SEGMENTS
    )


def require_admission(connection):
    if journal_storage.version(connection) == 3:
        from .journal_segments import summary

        # Check cold evidence once at admission, not on each streamed notification.
        summary(connection)
    report = capacity(connection)
    if not can_admit(report):
        if report["can_open_stream"]:
            raise ProtocolError("Protected journal retention capacity requires maintenance")
        raise ProtocolError("Private journal retention capacity requires archival")


def require_turn_capacity(connection, stream):
    row = connection.execute("SELECT * FROM streams WHERE id=?", (stream,)).fetchone()
    if row is None or row["state"] != "open":
        raise ProtocolError("Private journal stream unavailable")
    # A permit must cover even the maximum legal annotated intent before consumption.
    check_append(connection, row, ENTRY_LIMIT, reserve=ORDINARY_RESERVE)


def check_append(connection, stream, size, *, reserve):
    if (
        stream["bytes"] + size + reserve > STREAM_LIMIT
        or total_bytes(connection) + size + reserve > TOTAL_LIMIT
    ):
        raise ProtocolError("Private journal retention limit; cleanup space reserved")


def event_reserve(event, size):
    kind = event.get("kind")
    small = (
        kind in {"unknown", "work_stop"}
        or kind == "process"
        and event.get("phase") in {"stop_confirmed", "stop_unknown"}
    )
    if small:
        if size > SMALL_EVENT_LIMIT:
            raise ProtocolError("Private cleanup evidence size limit")
        if kind == "unknown" and set(event) != {"kind", "id"}:
            raise ProtocolError("Invalid unknown delivery evidence")
        return 0
    if kind == "response":
        return RESPONSE_RESERVE
    if kind == "work_terminal":
        return TERMINAL_RESERVE
    return ORDINARY_RESERVE

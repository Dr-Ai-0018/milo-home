"""Read the original payload bytes from immutable segments and hot overlays."""

import hashlib
import json
import re
from itertools import islice

from .codex_config import private_directory, private_read
from .codex_rpc import ProtocolError
from .journal_storage import (
    MAX_ARCHIVES,
    SEGMENTS,
    archives,
    canonical,
    directory,
    fingerprint,
    highwater,
    version,
)
from .work_stop import recorded_identity, stop_shape

SEGMENT_LIMIT = 20 * 1024 * 1024
STORAGE_LIMIT = 256 * 1024 * 1024
MAX_SEGMENTS = MAX_ARCHIVES
MAX_ENTRIES = 1000000


def inventory(connection, *, budget=STORAGE_LIMIT):
    records = archives(connection)
    root = directory(connection) / SEGMENTS
    if not root.exists() and not root.is_symlink():
        if records:
            raise ProtocolError("Journal segment directory is missing")
        return records
    private_directory(root)
    if version(connection) != 3:
        raise ProtocolError("Unregistered journal segment directory")
    names = {record["name"] for record in records}
    if len(records) > MAX_SEGMENTS or len(names) != len(records):
        raise ProtocolError("Journal segment inventory exceeds its bound")
    # No staging or unreferenced final file can silently disappear from backup.
    actual = {SEGMENTS + "/" + path.name for path in islice(root.iterdir(), MAX_SEGMENTS + 1)}
    if actual != names:
        raise ProtocolError("Journal segment inventory has missing or unregistered evidence")
    for record in records:
        if (
            re.fullmatch(r"[0-9a-f]{32}", record["stream"]) is None
            or record["name"] != SEGMENTS + "/" + record["stream"] + ".json"
            or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None
            or record["fingerprint"] != fingerprint(connection)
            or re.fullmatch(r"[0-9a-f]{64}", str(record["settlement_digest"])) is None
            or any(
                type(record[key]) is not int or record[key] < 1
                for key in (
                    "file_bytes",
                    "payload_bytes",
                    "entry_count",
                    "first_sequence",
                    "last_sequence",
                )
            )
            or record["file_bytes"] > SEGMENT_LIMIT
            or record["payload_bytes"] > 8 * 1024 * 1024
            or record["last_sequence"] < record["first_sequence"]
        ):
            raise ProtocolError("Journal segment index differs from its original binding")
    if (
        sum(record["file_bytes"] for record in records) > min(budget, STORAGE_LIMIT)
        or sum(record["entry_count"] for record in records) > MAX_ENTRIES
    ):
        raise ProtocolError("Journal segment inventory exceeds its byte or entry budget")
    highwater(connection)
    return records


def read_segment(connection, record, *, watermark=None):
    data = private_read(directory(connection) / record["name"], limit=SEGMENT_LIMIT)
    if len(data) != record["file_bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise ProtocolError("Journal segment bytes differ from their index")
    value = json.loads(data)
    stream = connection.execute("SELECT * FROM streams WHERE id=?", (record["stream"],)).fetchone()
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "stream", "unit", "fingerprint", "entries", "settlement"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["stream"] != record["stream"]
        or stream is None
        or value["unit"] != stream["unit"]
        or value["fingerprint"] != record["fingerprint"]
        or hashlib.sha256(canonical(value["settlement"])).hexdigest() != record["settlement_digest"]
        or not isinstance(value["entries"], list)
        or not value["entries"]
        or len(value["entries"]) != record["entry_count"]
        or len(value["entries"]) > MAX_ENTRIES
    ):
        raise ProtocolError("Journal segment scope differs from its index")
    entries = []
    for item in value["entries"]:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or type(item[0]) is not int
            or item[0] <= 0
            or not isinstance(item[1], str)
            or not isinstance(json.loads(item[1]), dict)
            or (entries and item[0] <= entries[-1][0])
        ):
            raise ProtocolError("Journal segment entry sequence or payload is invalid")
        entries.append((item[0], item[1]))
    if (
        entries[0][0] != record["first_sequence"]
        or entries[-1][0] != record["last_sequence"]
        or sum(len(payload.encode()) for _, payload in entries) != record["payload_bytes"]
        or entries[-1][0] > (highwater(connection) if watermark is None else watermark)
    ):
        raise ProtocolError("Journal segment entry bounds differ from their index")
    validate_settlement(record, entries, value["settlement"])
    if value["settlement"]["scope"]["unit"] != stream["unit"]:
        raise ProtocolError("Archived process unit differs from its original stream")
    return entries, value["settlement"]


def validate_settlement(record, entries, settlement):
    scope = settlement.get("scope", {}) if isinstance(settlement, dict) else {}
    if (
        not isinstance(settlement, dict)
        or settlement.get("eligible") is not True
        or not isinstance(scope, dict)
        or settlement.get("blocked_by") != []
        or re.fullmatch(r"[0-9a-f]{64}", str(settlement.get("business_digest"))) is None
        or scope.get("kind") not in {"work", "native_reply"}
        or scope.get("stream_id") != record["stream"]
        or scope.get("binding_fingerprint") != record["fingerprint"]
    ):
        raise ProtocolError("Journal archived settlement scope is invalid")
    events = [(sequence, json.loads(payload)) for sequence, payload in entries]
    identity = recorded_identity(events, scope.get("unit"))
    stop = dict(events).get(scope.get("stop_sequence"))
    if (
        not isinstance(stop, dict)
        or identity != scope.get("process_identity")
        or stop != scope.get("stop")
        or type(scope.get("stop_sequence")) is not int
        or scope["stop_sequence"] != events[-1][0]
    ):
        raise ProtocolError("Journal archived process identity differs from original evidence")
    if stop.get("kind") == "work_stop":
        stop_shape(stop)
        if {key: stop[key] for key in identity} != identity:
            raise ProtocolError("Archived stop belongs to another process generation")
    elif not (
        stop.get("kind") == "process"
        and stop.get("phase") == "stop_confirmed"
        and stop.get("unit") == identity["unit"]
    ):
        raise ProtocolError("Archived process stop is not confirmed")


def catalogue(connection):
    records = inventory(connection)
    return [
        {
            **record,
            "has_overlay": connection.execute(
                "SELECT 1 FROM entries WHERE stream=? LIMIT 1", (record["stream"],)
            ).fetchone()
            is not None,
            "settlement": read_segment(connection, record)[1],
        }
        for record in records
    ]


def raw_entries(connection, stream, *, checked_records=None, watermark=None):
    cold = []
    if version(connection) == 3:
        records = inventory(connection) if checked_records is None else checked_records
        record = next((record for record in records if record["stream"] == stream), None)
        if record is not None:
            cold, _ = read_segment(connection, record, watermark=watermark)
    hot = [
        (row[0], row[1])
        for row in connection.execute(
            "SELECT sequence,payload FROM entries WHERE stream=? ORDER BY sequence", (stream,)
        )
    ]
    if cold and hot and hot[0][0] <= cold[-1][0]:
        raise ProtocolError("Journal hot overlay overlaps immutable history")
    return cold + hot


def summary(connection, *, budget=STORAGE_LIMIT):
    if version(connection) != 3:
        root = directory(connection) / SEGMENTS
        if root.exists() or root.is_symlink():
            raise ProtocolError("Unregistered journal segment directory")
        return None
    records = inventory(connection, budget=budget)
    watermark = highwater(connection)
    digest, seen, size, count = hashlib.sha256(), set(), 0, 0
    for stream in connection.execute("SELECT * FROM streams ORDER BY id"):
        stream_bytes = 0
        for sequence, payload in raw_entries(
            connection, stream["id"], checked_records=records, watermark=watermark
        ):
            count += 1
            stream_bytes += len(payload.encode())
            encoded = canonical([sequence, stream["id"], payload])
            size += len(encoded)
            if sequence in seen or size > budget or count > MAX_ENTRIES:
                raise ProtocolError("Journal logical history overlaps or exceeds its budget")
            seen.add(sequence)
            digest.update(len(encoded).to_bytes(8, "big") + encoded)
        if stream_bytes != stream["bytes"]:
            raise ProtocolError("Journal stream bytes differ from its complete history")
        metadata = canonical(dict(stream))
        size += len(metadata)
        if size > budget:
            raise ProtocolError("Journal metadata exceeds its logical byte budget")
        digest.update(len(metadata).to_bytes(8, "big") + metadata)
    for table, order in (("requests", "stream,request_id"), ("stream_work", "stream")):
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            metadata = canonical([table, dict(row)])
            size += len(metadata)
            if size > budget:
                raise ProtocolError("Journal metadata exceeds its logical byte budget")
            digest.update(len(metadata).to_bytes(8, "big") + metadata)
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise ProtocolError("Journal evidence contains orphaned associations")
    return {
        "schema": 3,
        "sequence_highwater": watermark,
        "archives": records,
        "entry_count": count,
        "payload_bytes": sum(row[0] for row in connection.execute("SELECT bytes FROM streams")),
        "logical_sha256": digest.hexdigest(),
    }

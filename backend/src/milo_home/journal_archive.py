"""Publish a private immutable base before atomically moving its hot payloads."""

import hashlib
import os
import re
from itertools import islice
from uuid import uuid4

from .codex_config import private_create, private_directory, private_read
from .codex_rpc import ProtocolError
from .journal_segments import (
    MAX_SEGMENTS,
    SEGMENT_LIMIT,
    STORAGE_LIMIT,
    inventory,
    raw_entries,
    read_segment,
    validate_settlement,
)
from .journal_storage import SEGMENTS, archives, canonical, fingerprint, version
from .journal_streams import snapshot


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(root, name, content):
    temporary = root / ("." + name + "." + uuid4().hex + ".staging")
    private_create(temporary, content)
    if private_read(temporary, limit=SEGMENT_LIMIT) != content:
        raise ProtocolError("Staged journal segment changed before publication")
    os.link(temporary, root / name, follow_symlinks=False)
    sync_directory(root)
    temporary.unlink()
    sync_directory(root)


def seal(journal, expected, settlement):
    """Storage primitive; the caller must verify historical business scope under its lease."""
    if journal.read_only or settlement.get("eligible") is not True or settlement.get("blocked_by"):
        raise ProtocolError("Only verified settled evidence may be archived")
    stream = expected["stream"]["id"]
    if re.fullmatch(r"[0-9a-f]{32}", stream) is None:
        raise ProtocolError("Exact original stream identity is required for archival")
    with journal._connect() as connection:
        if version(connection) != 3:
            raise ProtocolError("Explicit journal segmentation migration is required")
        records = inventory(connection)
        if any(record["stream"] == stream for record in records):
            raise ProtocolError("Original stream already has an immutable base")
        if snapshot(connection, stream) != expected:
            raise ProtocolError("Private evidence changed before archival")
        if expected["stream"]["state"] != "closed" or any(
            request["state"] != "acknowledged" for request in expected["requests"]
        ):
            raise ProtocolError("Unresolved private execution cannot be archived")
        entries = raw_entries(connection, stream)
        if (
            not entries
            or sum(len(payload.encode()) for _, payload in entries) != expected["stream"]["bytes"]
        ):
            raise ProtocolError("Original journal payload accounting differs")
        value = {
            "version": 1,
            "stream": stream,
            "unit": expected["stream"]["unit"],
            "fingerprint": fingerprint(connection),
            "entries": entries,
            "settlement": settlement,
        }
        content = canonical(value)
        if (
            len(content) > SEGMENT_LIMIT
            or len(records) >= MAX_SEGMENTS
            or sum(record["file_bytes"] for record in records) + len(content) > STORAGE_LIMIT
        ):
            raise ProtocolError("Journal segment storage budget exceeded")
        record = {
            "stream": stream,
            "name": SEGMENTS + "/" + stream + ".json",
            "sha256": hashlib.sha256(content).hexdigest(),
            "file_bytes": len(content),
            "payload_bytes": expected["stream"]["bytes"],
            "entry_count": len(entries),
            "first_sequence": entries[0][0],
            "last_sequence": entries[-1][0],
            "fingerprint": value["fingerprint"],
            "settlement_digest": hashlib.sha256(canonical(settlement)).hexdigest(),
        }
        validate_settlement(record, entries, settlement)
        if settlement["scope"]["unit"] != expected["stream"]["unit"]:
            raise ProtocolError("Archived settlement unit differs from its stream")
        root = journal.path.parent / SEGMENTS
        if not root.exists():
            root.mkdir(mode=0o700)
            sync_directory(root.parent)
        private_directory(root)
        publish(root, stream + ".json", content)
        read_segment(connection, record)
        connection.execute(
            "INSERT INTO stream_archives VALUES ("
            ":stream,:name,:sha256,:file_bytes,:payload_bytes,:entry_count,"
            ":first_sequence,:last_sequence,:fingerprint,:settlement_digest)",
            record,
        )
        connection.execute("DELETE FROM entries WHERE stream=?", (stream,))
    if journal.stream_snapshot(stream) != expected:
        raise ProtocolError("Archived journal differs from its original complete snapshot")
    return {"archived": True, "stream_id": stream, "segment_sha256": record["sha256"]}


def orphan_names(journal):
    with journal._connect() as connection:
        return _orphan_names(journal, connection)


def _orphan_names(journal, connection):
    registered = {record["name"].split("/")[1] for record in archives(connection)}
    root = journal.path.parent / SEGMENTS
    if not root.exists() and not root.is_symlink():
        return []
    private_directory(root)
    actual = {path.name for path in islice(root.iterdir(), 2 * MAX_SEGMENTS + 1)}
    if len(actual) > 2 * MAX_SEGMENTS:
        raise ProtocolError("Journal orphan inventory exceeds its count bound")
    if not registered <= actual:
        raise ProtocolError("A registered immutable segment is missing")
    return sorted(actual - registered)


def quarantine_orphans(journal, output):
    """Explicit reversible isolation; registered segments are never moved or deleted."""
    if journal.read_only:
        raise ProtocolError("Orphan isolation requires an explicit writable journal")
    with journal._connect() as connection:
        return _quarantine(journal, connection, output)


def _quarantine(journal, connection, output):
    names = _orphan_names(journal, connection)
    if not names or len(names) > MAX_SEGMENTS:
        raise ProtocolError("No bounded orphan segment set is available for isolation")
    home = journal.path.parent
    if output.resolve() != output or output == home or home in output.parents:
        raise ValueError("Orphan isolation requires a new independent private directory")
    private_directory(output.parent)
    original = {}
    total = 0
    for name in names:
        content = private_read(home / SEGMENTS / name, limit=SEGMENT_LIMIT)
        total += len(content)
        if total > STORAGE_LIMIT:
            raise ProtocolError("Orphan segment isolation exceeds its byte budget")
        original[name] = content
    output.mkdir(mode=0o700)
    sync_directory(output.parent)
    private_create(output / "INCOMPLETE", b"1\n")
    (output / "evidence").mkdir(mode=0o700)
    for name, content in original.items():
        private_create(output / "evidence" / name, content)
    manifest = {
        "source": str(home / SEGMENTS),
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in original.items()},
    }
    private_create(output / "manifest.json", canonical(manifest))
    for name, content in original.items():
        if (
            private_read(output / "evidence" / name, limit=SEGMENT_LIMIT) != content
            or private_read(home / SEGMENTS / name, limit=SEGMENT_LIMIT) != content
        ):
            raise ProtocolError("Orphan evidence changed during isolation; originals retained")
    sync_directory(output)
    if _orphan_names(journal, connection) != names:
        raise ProtocolError("Orphan segment references changed during isolation")
    for name in original:
        (home / SEGMENTS / name).unlink()
    sync_directory(home / SEGMENTS)
    (output / "INCOMPLETE").unlink()
    sync_directory(output)
    return {"isolated": len(original), "output": str(output), "original_bytes_retained": True}

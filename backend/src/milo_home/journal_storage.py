"""Version-aware sequence allocation and immutable segment reference metadata."""

import json
from pathlib import Path

from .codex_rpc import ProtocolError

SEGMENTS = "journal-segments"
MAX_ARCHIVES = 10000
ARCHIVE_SCHEMA = (
    "CREATE TABLE sequence_highwater(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "value INTEGER NOT NULL CHECK(value>=0))",
    "CREATE TABLE stream_archives(stream TEXT PRIMARY KEY REFERENCES streams(id), "
    "name TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, file_bytes INTEGER NOT NULL, "
    "payload_bytes INTEGER NOT NULL, entry_count INTEGER NOT NULL, "
    "first_sequence INTEGER NOT NULL, last_sequence INTEGER NOT NULL, "
    "fingerprint TEXT NOT NULL, settlement_digest TEXT NOT NULL)",
)


def version(connection):
    return connection.execute("PRAGMA user_version").fetchone()[0]


def directory(connection):
    return Path(connection.execute("PRAGMA database_list").fetchone()[2]).parent


def migrate(connection):
    current = version(connection)
    if current == 3:
        highwater(connection)
        return
    if current != 2:
        raise ProtocolError("Only an existing v2 journal can enable segmentation")
    for statement in ARCHIVE_SCHEMA:
        connection.execute(statement)
    maximum = connection.execute("SELECT coalesce(max(sequence),0) FROM entries").fetchone()[0]
    connection.execute("INSERT INTO sequence_highwater VALUES (1,?)", (maximum,))
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER immutable_archive_{operation.lower()} BEFORE {operation} "
            "ON stream_archives BEGIN SELECT RAISE(ABORT,'Archive index is immutable'); END"
        )
    connection.execute("PRAGMA user_version=3")


def archives(connection):
    if version(connection) != 3:
        return []
    rows = connection.execute(
        "SELECT * FROM stream_archives ORDER BY stream LIMIT ?", (MAX_ARCHIVES + 1,)
    ).fetchall()
    if len(rows) > MAX_ARCHIVES:
        raise ProtocolError("Journal archive index exceeds its count bound")
    return [dict(row) for row in rows]


def highwater(connection):
    rows = connection.execute("SELECT singleton,value FROM sequence_highwater").fetchall()
    floor = connection.execute(
        "SELECT max(value) FROM (SELECT coalesce(max(sequence),0) AS value FROM entries "
        "UNION ALL SELECT coalesce(max(last_sequence),0) FROM stream_archives)"
    ).fetchone()[0]
    if (
        len(rows) != 1
        or rows[0][0] != 1
        or type(rows[0][1]) is not int
        or not floor <= rows[0][1] <= 2**63 - 1
    ):
        raise ProtocolError("Journal sequence highwater differs from original evidence")
    return rows[0][1]


def insert_entry(connection, stream, payload):
    if version(connection) == 3:
        current = highwater(connection)
        if current == 2**63 - 1:
            raise ProtocolError("Journal global sequence space is exhausted")
        sequence = current + 1
        connection.execute("UPDATE sequence_highwater SET value=? WHERE singleton=1", (sequence,))
        connection.execute("INSERT INTO entries VALUES (?,?,?)", (sequence, stream, payload))
        return sequence
    return connection.execute(
        "INSERT INTO entries(stream,payload) VALUES (?,?)", (stream, payload)
    ).lastrowid


def fingerprint(connection):
    import hashlib

    row = connection.execute("SELECT value FROM binding WHERE singleton=1").fetchone()
    if row is None:
        raise ProtocolError("No persistent main binding")
    return hashlib.sha256(row[0].encode()).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

"""Private, byte-preserving evidence of earlier initialization attempts."""

import hashlib
import json
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID

from .codex_config import private_directory, private_read
from .codex_rpc import ProtocolError
from .work_creation_records import CHOICE, EVENTS, HISTORY, CreationRecords, choice_digest, encoded
from .work_native_absence import verify_native_absence
from .work_private_files import publish_file, sync_directory

MAX_HISTORY = 16
MAX_HISTORY_BYTES = 16 * 1024 * 1024
_CONTEXT = (
    "database",
    "database_device",
    "database_inode",
    "database_owner",
    "owner_id",
    "session_id",
    "workspace_id",
    "cwd",
)
_CHOICE_ROW = (
    "id",
    "session_id",
    "owner_revision",
    "workspace_revision",
    "created_at",
    "deadline_at",
    "supersedes_id",
    "authorization_id",
)


@dataclass(frozen=True)
class Retirement:
    choice: dict
    choice_bytes: bytes
    events: tuple[bytes, ...]

    @property
    def digest(self):
        return hashlib.sha256(self.events[-1]).hexdigest()

    @property
    def size(self):
        return len(self.choice_bytes) + sum(map(len, self.events))


def _canonical_uuid(value):
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _sha256(value):
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def read_retirement(home, *, live=False):
    from .work_creation_abandon import _configuration, _original_events

    raw_choice = private_read(home / CHOICE)
    choice = json.loads(raw_choice)
    if (
        not isinstance(choice, dict)
        or choice.get("schema") != 1
        or not _canonical_uuid(choice.get("id"))
        or any(
            not isinstance(choice.get(key), str)
            for key in ("cwd", "codex_home", "config_digest", "provider", "model")
        )
        or type(choice.get("created_at")) is not int
        or encoded(choice) != raw_choice
    ):
        raise ProtocolError("Retired creation choice differs")
    records = CreationRecords(home)
    identity, prefix, audit = _original_events(records, choice)
    if (
        audit is None
        or set(audit)
        != {
            "kind",
            "connection",
            "choice_digest",
            "original_events_digest",
            "observed_config_digest",
            "native_evidence_digest",
            "stopped_family",
            "thread_start_delivery",
            "checked_at",
        }
        or audit["connection"] != identity["unit"]
        or audit["choice_digest"] != choice_digest(choice)
        or audit["original_events_digest"] != prefix
        or audit["stopped_family"] != identity
        or audit["thread_start_delivery"] != "unknown"
        or type(audit["checked_at"]) is not int
        or audit["checked_at"] < choice["created_at"]
        or not _sha256(audit["observed_config_digest"])
        or not _sha256(audit["native_evidence_digest"])
    ):
        raise ProtocolError("Original creation lacks an exact retirement record")
    if live and (
        choice["codex_home"] != str(home)
        or audit["observed_config_digest"]
        != _configuration(SimpleNamespace(choice=choice, directory=home))
        or audit["native_evidence_digest"] != verify_native_absence(home)
    ):
        raise ProtocolError("Retired original home evidence differs")
    raw = tuple(
        private_read(records.path / f"{number:06d}.json", limit=512 * 1024)
        for number in range(1, len(records.events) + 1)
    )
    if tuple(map(encoded, records.events)) != raw:
        raise ProtocolError("Original creation event bytes differ")
    return Retirement(choice, raw_choice, raw)


def attempt_matches(row, choice):
    return all(row[key] == choice.get(key) for key in _CHOICE_ROW) and row[
        "choice_digest"
    ] == choice_digest(choice)


def attempt_rows(connection, session_id):
    attempts = connection.execute(
        "SELECT * FROM work_main_creations WHERE session_id=? ORDER BY id",
        (session_id,),
    ).fetchmany(MAX_HISTORY + 2)
    retirements = connection.execute(
        "SELECT r.* FROM work_main_attempt_retirements AS r "
        "JOIN work_main_creations AS c ON c.id=r.attempt_id WHERE c.session_id=? "
        "ORDER BY r.attempt_id",
        (session_id,),
    ).fetchmany(MAX_HISTORY + 1)
    grants = connection.execute(
        "SELECT g.* FROM work_main_attempt_grants AS g "
        "JOIN work_main_creations AS c ON c.id=g.predecessor_id WHERE c.session_id=? ORDER BY g.id",
        (session_id,),
    ).fetchmany(MAX_HISTORY + 1)
    return attempts, retirements, grants


def read_history(home, choice, attempts, retirements, grants):
    if len(attempts) > MAX_HISTORY + 1:
        raise ProtocolError("Creation history exceeds its attempt bound")
    rows = {row["id"]: row for row in attempts}
    receipts = {row["attempt_id"]: row["evidence_digest"] for row in retirements}
    permits = {row["id"]: row for row in grants}
    if not _canonical_uuid(choice.get("id")):
        raise ProtocolError("Invalid current creation attempt identity")
    current = rows.pop(choice["id"], None)
    if current is None or not attempt_matches(current, choice):
        raise ProtocolError("Current creation does not match its business attempt")
    result = {}
    total = 0
    root = home / HISTORY
    if rows:
        private_directory(root)
    while current["supersedes_id"] is not None:
        previous = current["supersedes_id"]
        grant = permits.pop(current["authorization_id"], None)
        ancestor = rows.pop(previous, None)
        receipt = receipts.pop(previous, None)
        if (
            not _canonical_uuid(previous)
            or ancestor is None
            or ancestor["state"] != "closed"
            or receipt is None
            or grant is None
            or grant["predecessor_id"] != previous
            or grant["new_choice_digest"] != current["choice_digest"]
        ):
            raise ProtocolError("Creation attempt chain is incomplete")
        path = root / previous
        private_directory(path)
        if {child.name for child in path.iterdir()} != {CHOICE, EVENTS}:
            raise ProtocolError("Unexpected retired creation archive material")
        evidence = read_retirement(path)
        if (
            not attempt_matches(ancestor, evidence.choice)
            or evidence.digest != receipt
            or any(evidence.choice.get(key) != choice.get(key) for key in _CONTEXT)
        ):
            raise ProtocolError("Retired creation does not match its business receipt")
        total += evidence.size
        if total > MAX_HISTORY_BYTES:
            raise ProtocolError("Creation history exceeds its byte bound")
        result[previous] = evidence
        current = ancestor
    if rows or receipts or permits or current["authorization_id"] is not None:
        raise ProtocolError("Unexpected branches or unused creation authorizations")
    if root.exists() or root.is_symlink():
        private_directory(root)
        if not result or {child.name for child in root.iterdir()} != result.keys():
            raise ProtocolError("Creation history directory differs from the business chain")
    return result


def verify_history(home, attempts, retirements, grants):
    from .work_creation_records import read_choice

    read_history(home, read_choice(home), attempts, retirements, grants)


def check_archive_bound(evidence):
    if (
        len(evidence) > MAX_HISTORY
        or sum(item.size for item in evidence.values()) > MAX_HISTORY_BYTES
    ):
        raise ProtocolError("Creation history exceeds its archive bound")


def publish_history(home, evidence):
    check_archive_bound(evidence)
    root = home / HISTORY
    root.mkdir(mode=0o700)
    for key, item in evidence.items():
        attempt = root / key
        attempt.mkdir(mode=0o700)
        publish_file(attempt / CHOICE, item.choice_bytes)
        records = attempt / EVENTS
        records.mkdir(mode=0o700)
        for number, data in enumerate(item.events, 1):
            publish_file(records / f"{number:06d}.json", data)
        sync_directory(attempt)
    sync_directory(root)
    sync_directory(home)

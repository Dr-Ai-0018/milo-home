"""Offline validation of native policy and routing evidence, retaining original bindings."""

import hashlib
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from uuid import UUID

from .native_messages import NativeMessageStore
from .native_policy import NativePolicy
from .native_session_files import sqlite_snapshot
from .workspace_archive_files import read_file

NATIVE_DATABASES = {"native-session-policy.sqlite3", "native-message-links.sqlite3"}
NATIVE_MARKERS = {name.removesuffix(".sqlite3") + ".required" for name in NATIVE_DATABASES}
NATIVE_FILES = NATIVE_DATABASES | NATIVE_MARKERS | {"AGENTS.md"}


def native_evidence_name(name):
    return name in NATIVE_FILES


def native_evidence_summary(home, files, journal, budget):
    """Snapshot inventoried bytes away from the source; never rebind a restored home."""
    names = set(files) & NATIVE_FILES
    if not names:
        return None
    rules = files.get("AGENTS.md")
    if rules and (not 0 < rules["bytes"] <= 32 * 1024):
        raise ValueError("Native instructions exceed their registered bounds")
    summary = {"instructions_sha256": rules["sha256"] if rules else None, "databases": {}}
    for name in ("native-session-policy.sqlite3", "native-message-links.sqlite3"):
        marker = name.removesuffix(".sqlite3") + ".required"
        if name not in names and marker not in names:
            continue
        if name not in names or marker not in names:
            raise ValueError("Native evidence database and required marker must both exist")
        if read_file(home, marker, 2)[0] != b"1\n":
            raise ValueError("Native evidence registration marker differs")
        with tempfile.TemporaryDirectory(prefix="milo-native-evidence-") as temporary:
            snapshot = Path(temporary) / name
            sqlite_snapshot(home / name, snapshot, budget, files)
            with closing(
                sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)
            ) as db:
                db.row_factory = sqlite3.Row
                versions = {1} if name == "native-session-policy.sqlite3" else {1, 2}
                if db.execute("PRAGMA user_version").fetchone()[0] not in versions:
                    raise ValueError("Unsupported native evidence schema")
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Native evidence contains orphaned routing records")
                digest, size = hashlib.sha256(), 0
                for statement in db.iterdump():
                    content = (statement + "\n").encode()
                    size += len(content)
                    if size > budget:
                        raise ValueError("Native evidence logical snapshot exceeds its budget")
                    digest.update(content)
                details = (
                    _policy_summary(db, journal, summary["instructions_sha256"])
                    if name == "native-session-policy.sqlite3"
                    else _message_summary(
                        db, journal, summary["databases"].get("native-session-policy.sqlite3")
                    )
                )
                summary["databases"][name] = {"logical_sha256": digest.hexdigest(), **details}
    if "native-message-links.sqlite3" in names and "native-session-policy.sqlite3" not in names:
        raise ValueError("Native message evidence requires its original policy ledger")
    return summary


def _policy_summary(db, journal, instructions):
    linked = journal.business_binding()
    previous = None
    digests = []
    for row in db.execute("SELECT revision,body,digest FROM revisions ORDER BY revision"):
        policy = NativePolicy.model_validate_json(row["body"])
        if (
            linked is None
            or policy.revision != (previous.revision + 1 if previous else 1)
            or row["revision"] != policy.revision
            or policy.encoded() != row["body"]
            or policy.digest != row["digest"]
            or policy.previous_digest != (previous.digest if previous else None)
            or (previous and policy.registered_at < previous.registered_at)
            or policy.baseline_fingerprint != journal.fingerprint()
            or str(policy.owner_id) != linked.owner_id
            or str(policy.session_id) != linked.session_id
        ):
            raise ValueError("Native policy revisions differ from their original binding")
        previous = policy
        digests.append(policy.digest)
    if previous is None or (
        previous.instructions_digest is not None and previous.instructions_digest != instructions
    ):
        raise ValueError("Native policy is missing its registered instructions")
    return {
        "revisions": previous.revision,
        "current_digest": previous.digest,
        "revision_digests": digests,
    }


def _message_summary(db, journal, policy):
    if policy is None:
        raise ValueError("Native message evidence requires its original policy ledger")
    store = NativeMessageStore(journal)
    count = 0
    unresolved = 0
    for row in db.execute("SELECT run_id FROM deliveries ORDER BY run_id"):
        saved = store._read(db, str(UUID(row[0])))
        if saved[0].policy_digest not in policy["revision_digests"]:
            raise ValueError("Native message refers to a missing policy revision")
        count += 1
        events = saved[1]
        unresolved += bool(events) and not (
            events[-1].kind == "stopped"
            and events[-1].stop_confirmed is True
            and any(event.kind == "terminal" for event in events)
        )
    return {"deliveries": count, "without_complete_terminal_and_stop": unresolved}

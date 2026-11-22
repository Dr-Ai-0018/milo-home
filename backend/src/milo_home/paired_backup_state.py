"""Bounded source fingerprints and cross-store associations for one backup generation."""

import hashlib
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict

from .backup_rehearsal import record_counts
from .codex_journal import PrivateJournal
from .codex_rpc import ProtocolError
from .database import CURRENT_SCHEMA_VERSION, Database
from .database_inspection import work_recovery_snapshot
from .native_session_files import inventory, verify_sqlite
from .runtime_evidence_backup import CLAIMS, EVENTS, HISTORY, JOURNAL, runtime_inventory
from .runtime_native_evidence import NATIVE_DATABASES, NATIVE_FILES
from .work_attempt_history import verify_history
from .work_creation_records import (
    CreationRecords,
    original_binding,
    process_states,
    read_choice,
    resume_verified,
)
from .workspace_archive_files import DEFAULT_BUDGET, json_bytes, write_file
from .workspace_backup import identities, object_bytes
from .workspace_git import expanded_bytes, snapshot, validate_repository

FIRST_PAIRED_SCHEMA = 18


def business_state(database, budget, *, archive_schema=None, native_replies=False):
    with database.connect() as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        expected_schema = CURRENT_SCHEMA_VERSION if archive_schema is None else archive_schema
        if (
            type(expected_schema) is not int
            or not FIRST_PAIRED_SCHEMA <= expected_schema <= CURRENT_SCHEMA_VERSION
            or version != expected_schema
        ):
            raise ValueError("Paired capture requires the existing current business schema")
        digest = hashlib.sha256()
        total = 0
        for statement in connection.iterdump():
            content = (statement + "\n").encode()
            total += len(content)
            if total > budget:
                raise ValueError("Business logical snapshot exceeds the byte budget")
            digest.update(content)
        result = {
            "schema": version,
            "sha256": digest.hexdigest(),
            "logical_bytes": total,
            "sqlite_bytes": connection.execute("PRAGMA page_count").fetchone()[0]
            * connection.execute("PRAGMA page_size").fetchone()[0],
            "record_counts": record_counts(connection),
            "bindings": [
                dict(r) for r in connection.execute("SELECT * FROM work_runtime_bindings")
            ],
            "sessions": [dict(r) for r in connection.execute("SELECT * FROM work_sessions")],
            "workspaces": [dict(r) for r in connection.execute("SELECT * FROM workspaces")],
            "starts": [
                dict(r)
                for r in connection.execute(
                    "SELECT s.stream_id,s.binding_id,w.id AS window_id,w.task_id,w.unit_name "
                    "FROM execution_start_intents s JOIN execution_windows w ON w.id=s.window_id "
                    "ORDER BY s.stream_id"
                )
            ],
            "creations": [dict(r) for r in connection.execute("SELECT * FROM work_main_creations")],
            "active_legacy": connection.execute(
                "SELECT count(*) FROM runs WHERE status IN ('queued','running','stopping')"
            ).fetchone()[0],
        }
        if version >= 19:
            result["retirements"] = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM work_main_attempt_retirements ORDER BY attempt_id"
                )
            ]
            result["grants"] = [
                dict(r)
                for r in connection.execute("SELECT * FROM work_main_attempt_grants ORDER BY id")
            ]
        if native_replies:
            from .paired_native_replies import reply_business_state

            result["native_replies"] = reply_business_state(connection)
        return result


def business_snapshot(database, destination, budget):
    """Read as the registered owner; touch root-private files only after UID restoration."""
    deadline = time.monotonic() + 20
    write_file(destination, b"")
    with closing(sqlite3.connect(":memory:")) as memory:
        with database.connect() as source:
            source.execute("PRAGMA query_only=ON")
            page_size = source.execute("PRAGMA page_size").fetchone()[0]

            def progress(status, remaining, pages):
                if time.monotonic() >= deadline or pages * page_size > budget:
                    raise ValueError("Business snapshot exceeds its time or byte budget")

            if source.execute("PRAGMA page_count").fetchone()[0] * page_size > budget:
                raise ValueError("Business snapshot exceeds its byte budget")
            source.backup(memory, pages=64, progress=progress, sleep=0.01)
        with closing(sqlite3.connect(destination)) as target:
            memory.backup(target, pages=64, progress=progress, sleep=0.01)
            target.execute("PRAGMA journal_mode=DELETE")
    verify_sqlite(destination)
    descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def rehearse_historical_business(source, destination, budget, schema):
    """Reopen an older paired component unchanged; this does not make it runnable."""
    if not FIRST_PAIRED_SCHEMA <= schema < CURRENT_SCHEMA_VERSION:
        raise ValueError("Unsupported historical paired business schema")
    business_snapshot(source, destination, budget)
    with Database(destination).connect() as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0] != schema:
            raise ValueError("Historical paired backup must keep its original schema")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Historical paired backup failed its foreign key check")
        recovery = work_recovery_snapshot(connection, int(time.time()))
        return {
            "rehearsal_only": True,
            "source_migrated": False,
            "copied_schema_version": schema,
            "schema_version": schema,
            "record_counts": record_counts(connection),
            "active_runs_retained": connection.execute(
                "SELECT count(*) FROM runs WHERE status IN ('queued','running','stopping')"
            ).fetchone()[0],
            "active_work_windows_retained": len(recovery["active_work_windows"]),
            "active_main_creations_retained": len(recovery["active_main_creations"]),
            "private_runtime_required": recovery["private_runtime_required"],
            "automatic_wakeup": False,
        }


def _check_creation_history(business, journal, linked, home, source_home):
    attempts = business["creations"]
    retirements, grants = business.get("retirements", []), business.get("grants", [])
    history = home / HISTORY
    if not (
        len(attempts) > 1
        or any(row.get("supersedes_id") is not None for row in attempts)
        or retirements
        or grants
        or history.exists()
        or history.is_symlink()
    ):
        return
    try:
        verify_history(home, attempts, retirements, grants)
        choice = read_choice(home)
        records = CreationRecords(home)
        binding = original_binding(records, choice)
        if (
            not any(row["id"] == choice["id"] and row["state"] == "closed" for row in attempts)
            or choice["codex_home"] != str(source_home)
            or any(
                choice[key] != getattr(linked, key)
                for key in (
                    "database",
                    "database_device",
                    "database_inode",
                    "owner_id",
                    "session_id",
                    "workspace_id",
                )
            )
            or journal.binding() != binding
            or not resume_verified(records, choice, binding)
            or set(process_states(records).values()) != {"stop_confirmed"}
        ):
            raise ValueError(
                "Paired current creation does not match its successful original binding"
            )
        recorded = journal.binding_record()
        if any(choice[key] != recorded[key] for key in ("config_digest", "codex_home")):
            raise ValueError("Paired current creation does not match the journal configuration")
    except (ProtocolError, OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError("Paired creation history or current binding differs") from error


def check_association(
    business,
    home,
    workspace,
    expected=None,
    *,
    source_home=None,
    reply_routes=None,
    budget=256 * 1024 * 1024,
):
    journal = PrivateJournal(home, initialize=False, read_only=True)
    linked = journal.business_binding()
    if linked is None or journal.binding().cwd != workspace:
        raise ValueError("Exact business/runtime/workspace association is required")
    identity = asdict(linked)
    if expected is not None and identity != expected:
        raise ValueError("Restored runtime differs from the original association")
    if (
        journal.fingerprint() != linked.fingerprint
        or len(business["bindings"]) != 1
        or any(
            business["bindings"][0][key] != identity[key]
            for key in ("session_id", "binding_id", "fingerprint")
        )
        or not any(
            s["id"] == linked.session_id
            and s["owner_id"] == linked.owner_id
            and s["workspace_id"] == linked.workspace_id
            for s in business["sessions"]
        )
        or not any(
            w["id"] == linked.workspace_id and w["root_path"] == str(workspace)
            for w in business["workspaces"]
        )
        or any(c["session_id"] != linked.session_id for c in business["creations"])
    ):
        raise ValueError("Business backup scope differs from the private runtime")
    _check_creation_history(business, journal, linked, home, source_home or home)
    streams = [
        {
            "stream_id": item["stream"]["id"],
            "window_id": item["work"]["window_id"] if item["work"] else None,
            "task_id": item["work"]["task_id"] if item["work"] else None,
            "unit_name": item["stream"]["unit"],
        }
        for item in journal.list_streams()
    ]
    expected_streams = [
        {k: v for k, v in row.items() if k != "binding_id"} for row in business["starts"]
    ]
    work_streams = [row for row in streams if row["window_id"] is not None]
    reply_streams = [row for row in streams if row["window_id"] is None]
    if work_streams != expected_streams or any(
        row["binding_id"] != linked.binding_id for row in business["starts"]
    ):
        raise ValueError("Original stream/window/task associations differ")
    from .paired_native_replies import check_reply_association

    active_native = check_reply_association(business, journal, reply_streams, reply_routes, budget)
    if business["active_legacy"] != active_native:
        raise ValueError("Business backup has an uncovered legacy execution")
    return identity


def native_exclusions(home, extra, *, budget=DEFAULT_BUDGET):
    # These exact records are preserved by the paired runtime component.
    names = [JOURNAL, JOURNAL + "-wal", JOURNAL + "-shm", *sorted(CLAIMS), EVENTS, HISTORY]
    names.extend(sorted(NATIVE_FILES))
    names.extend(name + suffix for name in sorted(NATIVE_DATABASES) for suffix in ("-wal", "-shm"))
    from .journal_storage import SEGMENTS

    if (home / SEGMENTS).exists() or (home / SEGMENTS).is_symlink():
        # The runtime component must have validated the complete reference graph.
        runtime_inventory(home, budget)
        names.append(SEGMENTS)
    return sorted(set(extra) | {name for name in names if (home / name).exists()})


def source_state(
    database,
    home,
    workspace,
    lease,
    *,
    include,
    exclude,
    native_exclude,
    budget,
    reply_directory=None,
):
    lease.verify()
    business = business_state(database, budget, native_replies=reply_directory is not None)
    routes = None
    if reply_directory is not None:
        from .reply_route_backup import route_inventory

        routes = route_inventory(
            reply_directory,
            business["native_replies"]["runs"],
            home,
            PrivateJournal(home, initialize=False, read_only=True).fingerprint(),
            budget,
        )
    linked = check_association(business, home, workspace, reply_routes=routes, budget=budget)
    metadata = os.fstat(lease.descriptor)
    if (
        linked["database"] != str(lease.canonical)
        or linked["database_device"] != metadata.st_dev
        or linked["database_inode"] != metadata.st_ino
        or database.path.resolve() != lease.canonical
    ):
        raise ValueError("Original business database identity differs")
    common, gitdir = validate_repository(workspace)
    state, staged, unstaged = snapshot(workspace, include, exclude, budget)
    git_state = {
        "state": state,
        "staged": hashlib.sha256(staged).hexdigest(),
        "unstaged": hashlib.sha256(unstaged).hexdigest(),
    }
    runtime = runtime_inventory(home, budget)
    native = inventory(home, native_exclusions(home, native_exclude, budget=budget), budget)
    expanded = expanded_bytes(workspace)
    estimate = 4 * (
        business["logical_bytes"]
        + business["sqlite_bytes"]
        + runtime["bytes"]
        + native["bytes"]
        + object_bytes(workspace)
        + sum(expanded.values())
        + sum(r["bytes"] for r in state["files"].values() if r)
        + len(staged)
        + len(unstaged)
        + 1024 * 1024
    )
    lease.verify()
    result = {
        "business": business,
        "binding": linked,
        "thread_id": journal_thread(home),
        "identities": identities((home, workspace, common, gitdir)),
        "runtime": runtime,
        "native": native,
        "workspace_sha256": hashlib.sha256(json_bytes(git_state)).hexdigest(),
        "estimated_peak_bytes": estimate,
    }
    if routes is not None:
        result["reply_routes"] = routes
        result["reply_source_identity"] = [
            reply_directory.stat().st_dev,
            reply_directory.stat().st_ino,
        ]
        result["estimated_peak_bytes"] += 4 * routes["bytes"]
    return result


def journal_thread(home):
    return PrivateJournal(home, initialize=False, read_only=True).binding().thread_id

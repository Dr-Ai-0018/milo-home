"""Small local observations; expensive work never runs from the HTTP route."""

import os
import sqlite3
import stat
import time
from datetime import UTC, datetime

from .codex_rpc import ProtocolError
from .errors import ApiError
from .execution_lock import ExecutionIdentityError, execution_lock
from .journal_capacity import STREAM_LIMIT, TOTAL_LIMIT
from .journal_retention import verify_identity
from .journal_segments import MAX_SEGMENTS, STORAGE_LIMIT
from .maintenance_backup import read_daily_backup_evidence
from .maintenance_models import BackupFact, CapacityFact, MaintenanceView, age_summary
from .native_messages import NativeMessageStore
from .native_session_files import DATABASE, METADATA, RECORD_ROOTS

OBSERVATION_SECONDS = 3600
DAILY_FRESH_SECONDS = 48 * 3600


def bounded_size(root, *, max_entries=25000, timeout=2):
    """Metadata only; partial results remain explicit lower bounds."""
    end = time.monotonic() + timeout
    total, allocated, count = 0, 0, 0
    complete, pending, seen = True, [root], set()
    while pending:
        path = pending.pop()
        try:
            metadata = path.lstat()
            count += 1
            if count > max_entries or time.monotonic() >= end:
                return total, allocated, False
            if stat.S_ISDIR(metadata.st_mode):
                with os.scandir(path) as entries:
                    for entry in entries:
                        if count + len(pending) >= max_entries or time.monotonic() >= end:
                            return total, allocated, False
                        pending.append(path / entry.name)
            elif stat.S_ISREG(metadata.st_mode):
                identity = (metadata.st_dev, metadata.st_ino)
                if identity not in seen:
                    seen.add(identity)
                    total += metadata.st_size
                    allocated += metadata.st_blocks * 512
            elif stat.S_ISLNK(metadata.st_mode):
                complete = False
        except OSError:
            complete = False
    return total, allocated, complete


def directory_fact(kind, path, *, limit=None):
    if path is None:
        return CapacityFact(kind=kind, reason="unavailable")
    used, allocated, complete = bounded_size(path)
    state, reason = ("normal", None) if complete else ("unknown", "incomplete")
    if limit is not None and used >= limit:
        state, reason = "blocked", "capacity_full"
    elif limit is not None and used >= limit * 0.8:
        state, reason = "attention", "capacity_near"
    return CapacityFact(
        kind=kind,
        used_bytes=used,
        allocated_bytes=allocated,
        complete=complete,
        limit_bytes=limit,
        state=state,
        reason=reason,
    )


def business_fact(database):
    used, allocated = 0, 0
    try:
        for suffix in ("", "-wal", "-shm"):
            path = database.path.with_name(database.path.name + suffix)
            if suffix and not path.exists():
                continue
            metadata = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Database metadata is not an ordinary file")
            used += metadata.st_size
            allocated += metadata.st_blocks * 512
    except (OSError, ValueError):
        return CapacityFact(kind="business", reason="unavailable")
    return CapacityFact(
        kind="business",
        used_bytes=used,
        allocated_bytes=allocated,
        state="normal",
        complete=True,
    )


def native_history_fact(home):
    used, allocated, complete = 0, 0, True
    try:
        with os.scandir(home) as entries:
            for index, entry in enumerate(entries):
                if index >= 256:
                    complete = False
                    break
                name = entry.name
                base = name[:-4] if name.endswith(("-wal", "-shm")) else name
                if name not in RECORD_ROOTS | METADATA and not DATABASE.fullmatch(base):
                    continue
                size, blocks, done = bounded_size(home / name)
                used, allocated, complete = used + size, allocated + blocks, complete and done
    except OSError:
        complete = False
    return CapacityFact(
        kind="native_history",
        used_bytes=used,
        allocated_bytes=allocated,
        state="normal" if complete else "unknown",
        complete=complete,
        reason=None if complete else "incomplete",
    )


def runtime_facts(runtime):
    kinds = ("journal_hot", "journal_cold", "native_mapping")
    try:
        with execution_lock(runtime.database) as lease:
            verify_identity(runtime, lease)
            capacity = runtime.journal.capacity()
            storage = runtime.journal.storage_summary()
            mapping = NativeMessageStore(runtime.journal).capacity()
            verify_identity(runtime, lease)
        used = capacity["used_bytes"]
        state, reason = "normal", None
        if not capacity["can_open_stream"]:
            state, reason = "blocked", "capacity_full"
        elif used >= TOTAL_LIMIT - 2 * STREAM_LIMIT:
            state, reason = "attention", "capacity_near"
        hot = CapacityFact(
            kind="journal_hot",
            state=state,
            reason=reason,
            used_bytes=used,
            limit_bytes=TOTAL_LIMIT,
            complete=True,
        )
        records = storage["archives"] if storage is not None else []
        cold_bytes = sum(record["file_bytes"] for record in records)
        full = cold_bytes >= STORAGE_LIMIT or len(records) >= MAX_SEGMENTS
        cold = CapacityFact(
            kind="journal_cold",
            state="blocked"
            if full
            else "attention"
            if cold_bytes >= STORAGE_LIMIT * 0.8
            else "normal",
            reason="protected_capacity"
            if full
            else "capacity_near"
            if cold_bytes >= STORAGE_LIMIT * 0.8
            else None,
            used_bytes=cold_bytes,
            limit_bytes=STORAGE_LIMIT,
            complete=True,
        )
        size = mapping.get("page_size")
        near = bool(size and mapping["used_pages"] >= mapping["limit_pages"] * 0.8)
        state = "unknown" if not size else "attention" if near else "normal"
        reason = "unavailable" if not size else "capacity_near" if near else None
        links = CapacityFact(
            kind="native_mapping",
            state=state if mapping["has_capacity"] else "blocked",
            reason=reason if mapping["has_capacity"] else "protected_capacity",
            used_bytes=mapping["used_pages"] * size if size else None,
            limit_bytes=mapping["limit_pages"] * size if size else None,
            complete=bool(size),
        )
        return [hot, cold, links]
    except ApiError as error:
        reason = "busy" if error.code == "worker_busy" else "evidence_invalid"
    except (OSError, ValueError, ProtocolError, ExecutionIdentityError, sqlite3.Error):
        reason = "evidence_invalid"
    return [CapacityFact(kind=kind, reason=reason) for kind in kinds]


def daily_fact(raw):
    success, outcome = raw["last_success_at"], raw["last_outcome"]
    state, reason = "unknown", "unavailable"
    if success is not None:
        state, reason = "normal", "not_associated"
    if outcome == "failed":
        state, reason = "attention", "last_attempt_failed"
    elif outcome == "unknown":
        state, reason = "unknown", "unavailable"
    return BackupFact(
        kind="daily",
        state=state,
        reason=reason,
        last_attempt_at=raw["last_attempt_at"],
        last_success_at=success,
        last_outcome=outcome,
        fresh_until=success + DAILY_FRESH_SECONDS if success is not None else None,
        snapshot_id=raw.get("snapshot_id"),
    )


def collect_summary(
    database,
    runtime,
    *,
    workspace,
    unit_runner,
    success_file,
    daily_unit=None,
    attachment_store=None,
    public_asset_store=None,
    backup_directory=None,
    development_directory=None,
    backup_facts=(),
    now=None,
):
    now = int(time.time()) if now is None else now
    facts = [business_fact(database), *runtime_facts(runtime)]
    facts.append(directory_fact("workspace", workspace))
    facts.append(
        directory_fact(
            "attachments",
            attachment_store.root if attachment_store else None,
            limit=attachment_store.max_bytes if attachment_store else None,
        )
    )
    facts.append(directory_fact("backups", backup_directory))
    facts.append(
        directory_fact(
            "public_assets",
            public_asset_store.root if public_asset_store else None,
            limit=public_asset_store.max_bytes if public_asset_store else None,
        )
    )
    facts.append(directory_fact("development", development_directory))
    # Native history has its own ownership and is never summed with journal bytes.
    facts.append(native_history_fact(runtime.journal.path.parent))
    daily = read_daily_backup_evidence(
        unit_runner=unit_runner,
        success_file=success_file,
        unit=daily_unit,
        now=datetime.fromtimestamp(now, UTC),
    )
    known = {item.kind: item for item in backup_facts if item.kind != "daily"}
    backups = [daily_fact(daily)]
    backups.extend(
        known.get(kind, BackupFact(kind=kind, reason="unavailable"))
        for kind in ("paired", "remote", "restoration")
    )
    summary = MaintenanceView(
        observed_at=now,
        expires_at=now + OBSERVATION_SECONDS,
        capacity=facts,
        backups=backups,
    )
    return age_summary(summary, now)

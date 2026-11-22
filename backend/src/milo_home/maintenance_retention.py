"""Bounded local maintenance of existing, fully settled journal streams.

The caller schedules this entry; it never schedules execution or changes an
archival qualification. Reports contain local identities, never event payloads.
"""

import math
import re
import shutil
import sqlite3
import time

from . import journal_retention
from .codex_rpc import ProtocolError
from .errors import ApiError
from .execution_lock import ExecutionIdentityError, execution_lock
from .journal_archive import orphan_names
from .journal_capacity import STREAM_LIMIT, TOTAL_LIMIT
from .journal_retention_scope import inspect_stream
from .journal_segments import MAX_SEGMENTS, SEGMENT_LIMIT, STORAGE_LIMIT, raw_entries
from .journal_storage import canonical, fingerprint, version
from .native_messages import NativeMessageStore

TRIGGER_BYTES = TOTAL_LIMIT - 2 * STREAM_LIMIT
MAX_STREAMS = 2
SCAN_LIMIT = 64
FREE_RESERVE_BYTES = 64 * 1024 * 1024


def _report():
    return {
        "status": "preview",
        "automatic_execution_requested": False,
        "trigger_bytes": TRIGGER_BYTES,
        "max_streams": MAX_STREAMS,
        "blocked_by": [],
        "state": None,
        "candidates": [],
        "archived": [],
        "scan_complete": True,
        "next_after": None,
    }


def _validate(limit, after, timeout):
    if type(limit) is not int or not 1 <= limit <= SCAN_LIMIT:
        raise ValueError("Maintenance scan limit must be between 1 and 64")
    if after is not None and (
        not isinstance(after, str) or re.fullmatch(r"[0-9a-f]{32}", after) is None
    ):
        raise ValueError("Maintenance cursor must identify an original stream")
    if not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("Maintenance timeout must be positive and at most 30 seconds")


def _failure(report, error):
    if isinstance(error, ApiError) and error.code == "worker_busy":
        report.update(status="deferred", blocked_by=["worker_busy"])
    else:
        reason = (
            "maintenance_identity_invalid"
            if isinstance(error, ExecutionIdentityError)
            else "maintenance_evidence_unavailable"
        )
        report.update(status="blocked", blocked_by=[reason])
    return report


def _state(runtime, lease):
    journal_retention.verify_identity(runtime, lease)
    journal = runtime.journal
    orphans = orphan_names(journal)
    if orphans:
        return {"orphan_count": len(orphans)}, ["unregistered_segment_evidence"]
    capacity = journal.capacity()
    storage = journal.storage_summary()
    mapping = NativeMessageStore(journal).capacity()
    state = {
        "capacity": capacity,
        "storage": storage,
        "mapping": mapping,
        "free_bytes": shutil.disk_usage(journal.path.parent).free,
        "segmentation_required": storage is None,
    }
    blocked = []
    if not mapping["has_capacity"]:
        blocked.append("native_mapping_capacity")
    if capacity.get("archived_bytes", 0) >= STORAGE_LIMIT:
        blocked.append("cold_storage_capacity")
    if capacity.get("archived_streams", 0) >= MAX_SEGMENTS:
        blocked.append("cold_segment_count")
    journal_retention.verify_identity(runtime, lease)
    return state, blocked


def _streams(journal, after, limit):
    with journal._connect() as connection:
        archived = (
            " AND id NOT IN (SELECT stream FROM stream_archives)"
            if version(connection) == 3
            else ""
        )
        return [
            dict(row)
            for row in connection.execute(
                "SELECT id,state,bytes FROM streams WHERE id>?" + archived + " ORDER BY id LIMIT ?",
                (after or "", limit + 1),
            )
        ]


def _candidate(runtime, row, lease, reply_directory):
    candidate = {
        "stream_id": row["id"],
        "hot_bytes": row["bytes"],
        "eligible": False,
        "blocked_by": [],
        "segment_bytes": None,
    }
    admission = inspect_stream(runtime, row["id"], lease=lease, reply_directory=reply_directory)
    candidate.update(eligible=admission["eligible"], blocked_by=admission["blocked_by"])
    if admission["eligible"]:
        with runtime.journal._connect() as connection:
            stream = connection.execute(
                "SELECT unit FROM streams WHERE id=?", (row["id"],)
            ).fetchone()
            candidate["segment_bytes"] = len(
                canonical(
                    {
                        "version": 1,
                        "stream": row["id"],
                        "unit": stream[0],
                        "fingerprint": fingerprint(connection),
                        "entries": raw_entries(connection, row["id"]),
                        "settlement": admission,
                    }
                )
            )
    return candidate


def _space_reasons(state, candidate):
    size = candidate["segment_bytes"]
    if size > SEGMENT_LIMIT:
        return ["cold_segment_size"]
    if state["capacity"].get("archived_bytes", 0) + size > STORAGE_LIMIT:
        return ["cold_storage_capacity"]
    if state["free_bytes"] < size + FREE_RESERVE_BYTES:
        return ["maintenance_disk_space"]
    return []


def _preview(runtime, *, reply_directory, limit, after, end, only_when_due, candidate_limit=None):
    report = _report()
    try:
        with execution_lock(runtime.database) as lease:
            state, blocked = _state(runtime, lease)
            report["state"] = state
            if blocked:
                report.update(status="blocked", blocked_by=blocked)
                return report
            if only_when_due and state["capacity"]["used_bytes"] < TRIGGER_BYTES:
                report["status"] = "below_threshold"
                return report
            streams = _streams(runtime.journal, after, limit)
            examined = []
            for row in streams[:limit]:
                if time.monotonic() >= end:
                    report.update(status="deferred", blocked_by=["maintenance_deadline"])
                    break
                candidate = _candidate(runtime, row, lease, reply_directory)
                if candidate["eligible"]:
                    candidate["maintenance_blocked_by"] = _space_reasons(state, candidate)
                examined.append(candidate)
                if (
                    candidate_limit
                    and sum(item["eligible"] for item in examined) >= candidate_limit
                ):
                    break
            report["candidates"] = examined
            report["scan_complete"] = len(examined) == len(streams)
            if not report["scan_complete"]:
                report["next_after"] = examined[-1]["stream_id"] if examined else after
            journal_retention.verify_identity(runtime, lease)
    except (
        ApiError,
        ExecutionIdentityError,
        ProtocolError,
        OSError,
        ValueError,
        sqlite3.Error,
    ) as error:
        return _failure(report, error)
    return report


def preview(runtime, *, reply_directory=None, limit=SCAN_LIMIT, after=None, timeout=20):
    """Read-only eligibility and exact segment sizes, including below the trigger.

    Continue an incomplete scan with next_after; None at the end starts a fresh
    scan. The bounded scan excludes archived bases, so old archives cannot starve
    new candidates. No v2 migration or write is performed by this operation.
    """
    _validate(limit, after, timeout)
    return _preview(
        runtime,
        reply_directory=reply_directory,
        limit=limit,
        after=after,
        end=time.monotonic() + timeout,
        only_when_due=False,
    )


def _preflight(runtime, stream_id, reply_directory):
    with execution_lock(runtime.database) as lease:
        state, blocked = _state(runtime, lease)
        if blocked or state["capacity"]["used_bytes"] < TRIGGER_BYTES:
            return state, blocked, False
        candidate = _candidate(runtime, {"id": stream_id, "bytes": 0}, lease, reply_directory)
        if not candidate["eligible"]:
            return state, ["candidate_changed", *candidate["blocked_by"]], False
        return state, _space_reasons(state, candidate), True


def run(
    runtime,
    *,
    reply_directory=None,
    limit=SCAN_LIMIT,
    after=None,
    max_streams=MAX_STREAMS,
    timeout=20,
):
    """Archive at most two eligible streams when due, using the original entry.

    Each archive reacquires the common nonblocking lock and revalidates all
    original qualifications. A preview is never authorization or a retained
    lease. Deadlines are checked between operations; the caller must retain its
    process-level runtime bound for an individual filesystem/SQLite operation.
    """
    _validate(limit, after, timeout)
    if type(max_streams) is not int or not 1 <= max_streams <= MAX_STREAMS:
        raise ValueError("Maintenance archives at most two streams per run")
    end = time.monotonic() + timeout
    report = _preview(
        runtime,
        reply_directory=reply_directory,
        limit=limit,
        after=after,
        end=end,
        only_when_due=True,
        candidate_limit=max_streams,
    )
    if report["status"] != "preview":
        return report
    for candidate in report["candidates"]:
        if not candidate["eligible"]:
            continue
        if time.monotonic() >= end:
            report.update(status="deferred", blocked_by=["maintenance_deadline"])
            return report
        try:
            state, blocked, due = _preflight(runtime, candidate["stream_id"], reply_directory)
            report["state"] = state
            if blocked:
                report.update(status="blocked", blocked_by=blocked)
                return report
            if not due:
                break
            result = journal_retention.archive(
                runtime, candidate["stream_id"], reply_directory=reply_directory
            )
            if not result["archived"]:
                report.update(status="blocked", blocked_by=result["blocked_by"])
                return report
            report["archived"].append(result)
        except (
            ApiError,
            ExecutionIdentityError,
            ProtocolError,
            OSError,
            ValueError,
            sqlite3.Error,
        ) as error:
            return _failure(report, error)
        if len(report["archived"]) >= max_streams:
            break
    report["status"] = "maintained" if report["archived"] else "no_eligible_candidates"
    try:
        with execution_lock(runtime.database) as lease:
            report["state"], blocked = _state(runtime, lease)
            if blocked:
                report.update(status="blocked", blocked_by=blocked)
    except (
        ApiError,
        ExecutionIdentityError,
        ProtocolError,
        OSError,
        ValueError,
        sqlite3.Error,
    ) as error:
        return _failure(report, error)
    return report

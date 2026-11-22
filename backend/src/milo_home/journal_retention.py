"""Explicit host-locked inspection and archival; never starts or republishes work."""

import os

from .codex_rpc import ProtocolError
from .execution_lock import execution_lock
from .journal_archive import orphan_names, quarantine_orphans, seal
from .journal_retention_scope import inspect_stream


def verify_identity(runtime, lease):
    lease.verify()
    linked = runtime.journal.business_binding()
    metadata = os.fstat(lease.descriptor)
    if linked is None or (linked.database, linked.database_device, linked.database_inode) != (
        str(lease.canonical),
        metadata.st_dev,
        metadata.st_ino,
    ):
        raise ProtocolError("Journal retention belongs to another business database")


def inspect(runtime, *, stream_id=None, reply_directory=None):
    with execution_lock(runtime.database) as lease:
        verify_identity(runtime, lease)
        orphans = orphan_names(runtime.journal)
        result = {"automatic_execution_requested": False, "orphan_segments": orphans}
        if orphans:
            return {**result, "blocked_by": ["unregistered_segment_evidence"]}
        result["capacity"] = runtime.journal.capacity()
        result["storage"] = runtime.journal.storage_summary()
        if stream_id is not None:
            result["admission"] = inspect_stream(
                runtime, stream_id, lease=lease, reply_directory=reply_directory
            )
        verify_identity(runtime, lease)
        return result


def archive(runtime, stream_id, *, reply_directory=None):
    with execution_lock(runtime.database) as lease:
        verify_identity(runtime, lease)
        journal = runtime.journal
        before = inspect_stream(runtime, stream_id, lease=lease, reply_directory=reply_directory)
        if not before["eligible"]:
            return {"archived": False, "blocked_by": before["blocked_by"]}
        if any(record["stream"] == stream_id for record in journal.segment_catalogue()):
            return {"archived": False, "blocked_by": ["already_archived"]}
        expected = journal.stream_snapshot(stream_id)
        journal.enable_segmentation()
        confirmed = inspect_stream(runtime, stream_id, lease=lease, reply_directory=reply_directory)
        if before != confirmed:
            raise ProtocolError("Historical business evidence changed before archival")
        result = seal(journal, expected, before)
        after = inspect_stream(runtime, stream_id, lease=lease, reply_directory=reply_directory)
        if after != before:
            raise ProtocolError("Historical business evidence changed during archival")
        verify_identity(runtime, lease)
        return result


def isolate(runtime, output):
    with execution_lock(runtime.database) as lease:
        verify_identity(runtime, lease)
        result = quarantine_orphans(runtime.journal, output)
        verify_identity(runtime, lease)
        return result

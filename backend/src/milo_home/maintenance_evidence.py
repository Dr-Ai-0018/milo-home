"""Preserve distinct local backup facts without renewing their original dates."""

import time

from .maintenance_cache import publish_summary
from .maintenance_models import BackupFact, MaintenanceDocument, MaintenanceView
from .maintenance_policy import trusted_bytes

FACT_FRESH_SECONDS = 7 * 24 * 3600


def evidence_path(settings):
    return settings.file.with_name("backup-facts.json")


def load_facts(settings, database):
    try:
        document = MaintenanceDocument.model_validate_json(
            trusted_bytes(evidence_path(settings), limit=64 * 1024)
        )
    except FileNotFoundError:
        return []
    identity = database.path.stat()
    if document.owner_id != settings.owner or (
        document.database_device,
        document.database_inode,
    ) != (identity.st_dev, identity.st_ino):
        return []
    return document.summary.backups


def record_fact(settings, database, kind, report, *, now=None):
    now = int(time.time()) if now is None else now
    known = {fact.kind: fact for fact in load_facts(settings, database)}
    old = known.get(kind, BackupFact(kind=kind))
    attempt = report.get("last_attempt_at", now)
    if old.last_attempt_at is not None and attempt < old.last_attempt_at:
        raise ValueError("Older maintenance evidence cannot replace a newer attempt")
    success = report.get("last_success_at")
    outcome = report.get("last_outcome", "unknown")
    new_success = success is not None
    generation = report.get("generation_id") if new_success else old.generation_id
    same_generation = generation is not None and generation == old.generation_id
    snapshot = report.get("snapshot_id") if new_success else old.snapshot_id
    if snapshot is None and new_success and same_generation:
        snapshot = old.snapshot_id
    reasons = {
        "classification_required": "unclassified_files",
        "budget_insufficient": "insufficient_space",
        "disk_insufficient": "insufficient_space",
        "writers_unconfirmed": "unavailable",
    }
    reason = None
    if outcome == "deferred":
        reason = reasons.get(report.get("reason"), "busy")
    elif outcome == "failed":
        reason = "last_attempt_failed"
    elif outcome == "unknown":
        reason = "unavailable"
    fact = BackupFact(
        kind=kind,
        last_attempt_at=attempt,
        last_success_at=success if success is not None else old.last_success_at,
        last_outcome=outcome,
        snapshot_id=snapshot,
        generation_id=generation,
        fresh_until=success + FACT_FRESH_SECONDS if success is not None else old.fresh_until,
        state="normal"
        if outcome == "succeeded"
        else "unknown"
        if outcome == "unknown"
        else "attention",
        reason=reason,
    )
    known[kind] = fact
    identity = database.path.stat()
    document = MaintenanceDocument(
        owner_id=settings.owner,
        database_device=identity.st_dev,
        database_inode=identity.st_ino,
        summary=MaintenanceView(
            observed_at=now,
            expires_at=now + FACT_FRESH_SECONDS,
            backups=list(known.values()),
        ),
    )
    publish_summary(evidence_path(settings), document)
    return fact

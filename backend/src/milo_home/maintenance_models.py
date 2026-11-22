"""Public maintenance facts; paths, command output and credentials stay private."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from .schemas import InputModel

FactState = Literal["unknown", "normal", "attention", "blocked"]
CapacityKind = Literal[
    "business",
    "attachments",
    "public_assets",
    "workspace",
    "journal_hot",
    "journal_cold",
    "native_mapping",
    "native_history",
    "backups",
    "development",
]
EvidenceKind = Literal["daily", "paired", "remote", "restoration"]
MaintenanceReason = Literal[
    "unavailable",
    "expired",
    "incomplete",
    "capacity_near",
    "capacity_full",
    "protected_capacity",
    "evidence_invalid",
    "busy",
    "last_attempt_failed",
    "not_associated",
    "unclassified_files",
    "insufficient_space",
]


class CapacityFact(InputModel):
    kind: CapacityKind
    state: FactState = "unknown"
    used_bytes: int | None = Field(default=None, ge=0)
    allocated_bytes: int | None = Field(default=None, ge=0)
    limit_bytes: int | None = Field(default=None, gt=0)
    complete: bool = False
    reason: MaintenanceReason | None = None


class BackupFact(InputModel):
    kind: EvidenceKind
    state: FactState = "unknown"
    last_attempt_at: int | None = Field(default=None, ge=0)
    last_success_at: int | None = Field(default=None, ge=0)
    last_outcome: Literal["unknown", "succeeded", "failed", "deferred"] = "unknown"
    fresh_until: int | None = Field(default=None, ge=0)
    snapshot_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    generation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    reason: MaintenanceReason | None = None


class MaintenanceView(InputModel):
    version: Literal[1] = 1
    observed_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)
    state: FactState = "unknown"
    capacity: list[CapacityFact] = Field(default_factory=list, max_length=10)
    backups: list[BackupFact] = Field(default_factory=list, max_length=4)


class MaintenanceDocument(InputModel):
    """Local envelope binds the sanitized report to the selected owner and DB."""

    owner_id: UUID
    database_device: int = Field(ge=0)
    database_inode: int = Field(ge=0)
    summary: MaintenanceView


def unknown_summary(now: int) -> MaintenanceView:
    return MaintenanceView(observed_at=now, expires_at=now)


def age_summary(summary: MaintenanceView, now: int) -> MaintenanceView:
    """Reading a cached observation never renews its validity."""
    summary = summary.model_copy(deep=True)
    expired = not summary.observed_at <= now < summary.expires_at
    if expired:
        summary.state = "unknown"
        for fact in summary.capacity:
            fact.state, fact.reason = "unknown", "expired"
        for fact in summary.backups:
            fact.state, fact.reason = "unknown", "expired"
        return summary
    for fact in summary.backups:
        if fact.state == "normal" and fact.fresh_until is not None and now >= fact.fresh_until:
            fact.state, fact.reason = "attention", "expired"
    states = [fact.state for fact in [*summary.capacity, *summary.backups]]
    summary.state = next(
        (state for state in ("blocked", "attention", "unknown") if state in states),
        "normal" if states else "unknown",
    )
    return summary

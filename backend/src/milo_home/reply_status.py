"""Explain queue evidence without treating receipt or service configuration as activity."""

from typing import Literal

from pydantic import BaseModel

from .execution_control import execution_busy
from .reply_scope import ReplyQueueCounts

ReplyWaitingReason = Literal[
    "nothing_pending",
    "clock_invalid",
    "schedule_unconfigured",
    "paused",
    "resting",
    "winding_down",
    "processing",
    "stopping",
    "needs_reconciliation",
    "execution_busy",
    "blocked",
    "retry_wait",
    "waiting",
    "automatic_unknown",
]


class ReplyQueueView(ReplyQueueCounts):
    reason: ReplyWaitingReason


class ReplyServiceView(BaseModel):
    state: Literal["unknown", "scheduled", "disabled", "faulted"] = "unknown"
    reason: Literal[
        "observation_missing",
        "check_current",
        "explicitly_disabled",
        "execution_fault",
        "observation_expired",
        "clock_or_boot_invalid",
    ] = "observation_missing"
    observed_at: int | None = None
    valid_until: int | None = None


def occupancy_reason(connection, now: int):
    if not execution_busy(connection):
        return None
    uncertain = connection.execute(
        "SELECT 1 FROM runs WHERE status IN ('queued','running','stopping') "
        "AND (created_at>? OR deadline_at<=?) "
        "UNION ALL SELECT 1 FROM execution_windows WHERE state!='closed' "
        "AND (state='unknown' OR created_at>? OR deadline_at<=?) "
        "UNION ALL SELECT 1 FROM work_main_creations WHERE state!='closed' "
        "AND (state='unknown' OR created_at>? OR deadline_at<=?) LIMIT 1",
        (now, now, now, now, now, now),
    ).fetchone()
    return "needs_reconciliation" if uncertain else "execution_busy"


def queue_view(counts, phase, processing, automatic, occupancy, *, clock_invalid=False):
    if not counts.pending_count:
        reason = "nothing_pending"
    elif counts.clock_invalid or clock_invalid:
        reason = "clock_invalid"
    elif processing == "stopping":
        reason = "stopping"
    elif processing in {"queued", "running"}:
        reason = "processing"
    elif processing == "unknown":
        reason = "needs_reconciliation"
    elif occupancy == "needs_reconciliation":
        reason = "needs_reconciliation"
    elif phase in {"unconfigured", "unknown"}:
        reason = "schedule_unconfigured"
    elif phase in {"paused", "resting", "winding_down"}:
        reason = phase
    elif counts.blocked_count == counts.pending_count:
        reason = "blocked"
    elif occupancy:
        reason = "execution_busy"
    elif counts.reserved_count:
        reason = "needs_reconciliation"
    elif not counts.available_count and counts.retry_wait_count:
        reason = "retry_wait"
    else:
        reason = "waiting" if automatic == "enabled" else "automatic_unknown"
    values = counts.model_dump()
    values["clock_invalid"] = counts.clock_invalid or clock_invalid
    return ReplyQueueView(**values, reason=reason)

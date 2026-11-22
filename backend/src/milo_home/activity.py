"""Owner-scoped observations; absence of evidence never implies online presence."""

import time
from typing import Literal

from pydantic import BaseModel

from .automatic_services import service_database_matches
from .conversations import ConversationService
from .execution_pulse import PULSE_TTL, pulse_consistent
from .interaction_models import ReplySummary
from .interaction_timing import ReceptionView, RevisitSummary
from .management_lease import current_boot, observe_management
from .reception import observe
from .reply_decisions import conversation_summary, revisit_summary
from .reply_scope import queue_counts
from .reply_status import ReplyQueueView, ReplyServiceView, occupancy_reason, queue_view
from .schedule_policy import describe
from .schedules import schedule_snapshot
from .service_observation import reply_service_view


class ActivityView(BaseModel):
    observed_at: int
    valid_until: int
    response_phase: Literal["working", "winding_down", "resting", "paused", "unknown"]
    processing: Literal["idle", "queued", "running", "stopping", "unknown"]
    heartbeat_at: int | None
    latest_reply_at: int | None
    reply_queue: ReplyQueueView | None = None
    reply_service: ReplyServiceView | None = None
    reply_summary: ReplySummary | None = None
    reception: ReceptionView | None = None
    revisits: RevisitSummary | None = None
    automatic_processing: Literal["enabled", "stopped", "unknown"]
    automatic_reason: Literal[
        "management_not_configured",
        "management_lease_current",
        "management_stopped",
        "management_lease_expired",
        "management_clock_or_boot_invalid",
    ]


class ActivityService:
    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock

    def view(self, owner: str, conversation_id: str) -> ActivityView:
        now = int(self.clock())
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            conversation = ConversationService._require_owner(connection, owner, conversation_id)
            policy, state = schedule_snapshot(connection)
            schedule = describe(policy, state, now)
            run = connection.execute(
                "SELECT runs.id AS run_id, runs.status, runs.stop_reason, runs.created_at, "
                "runs.started_at AS run_started_at, runs.deadline_at, runs.unit_name, "
                "run_activity.started_at, run_activity.heartbeat_at, run_activity.clock_invalid "
                "FROM runs LEFT JOIN run_activity ON run_activity.run_id = runs.id "
                "WHERE runs.conversation_id = ? "
                "AND runs.status IN ('queued', 'running', 'stopping')",
                (conversation_id,),
            ).fetchone()
            latest = connection.execute(
                "SELECT max(created_at) FROM messages WHERE conversation_id = ? AND role = 'milo'",
                (conversation_id,),
            ).fetchone()[0]
            management = connection.execute(
                "SELECT * FROM management_lease WHERE id = 1"
            ).fetchone()
            counts = queue_counts(
                connection, conversation["thread_id"], now, conversation_id=conversation_id
            )
            summary = conversation_summary(connection, conversation_id)
            revisits = revisit_summary(connection, conversation_id, now)
            occupancy = occupancy_reason(connection, now)
            service = connection.execute(
                "SELECT * FROM automatic_services WHERE kind='reply'"
            ).fetchone()
            if not service_database_matches(connection, service):
                service = None
            reception = observe(connection, owner, "reply", now, now + PULSE_TTL)
        phase = schedule.phase
        if phase == "unconfigured" or state["updated_at"] > now:
            phase = "unknown"
        until = now + PULSE_TTL
        if schedule.next_change_at is not None:
            until = min(until, schedule.next_change_at)
        processing, pulse = observe_run(run, now)
        if processing in {"queued", "running"}:
            until = min(until, run["deadline_at"])
        if pulse is not None:
            until = min(until, pulse + PULSE_TTL)
        automatic, reason, management_until = observe_management(management, now, current_boot())
        if management_until is not None:
            until = min(until, management_until)
        if counts.retry_after is not None:
            until = min(until, counts.retry_after)
        if revisits.next_due_at is not None and revisits.next_due_at > now:
            until = min(until, revisits.next_due_at)
        if reception is not None:
            until = min(until, reception.valid_until)
            reception = reception.model_copy(update={"valid_until": until})
        return ActivityView(
            observed_at=now,
            valid_until=until,
            response_phase=phase,
            processing=processing,
            heartbeat_at=pulse,
            latest_reply_at=latest if latest is not None and latest <= now else None,
            reply_queue=queue_view(
                counts,
                phase,
                processing,
                automatic,
                occupancy,
                clock_invalid=state["updated_at"] > now,
            ),
            reply_service=reply_service_view(service, owner, now, current_boot()),
            reply_summary=summary,
            reception=reception,
            revisits=revisits,
            automatic_processing=automatic,
            automatic_reason=reason,
        )


def observe_run(run, now: int) -> tuple[str, int | None]:
    if run is None:
        return "idle", None
    if run["created_at"] > now:
        return "unknown", None
    if run["status"] == "stopping" or run["stop_reason"]:
        return "stopping", None
    if run["deadline_at"] is None or now >= run["deadline_at"]:
        return "unknown", None
    if run["status"] == "queued":
        return "queued", None
    if (
        run["heartbeat_at"] is not None
        and pulse_consistent(run, now)
        and now < run["heartbeat_at"] + PULSE_TTL
    ):
        return "running", run["heartbeat_at"]
    return "unknown", None

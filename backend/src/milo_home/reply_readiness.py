"""Read-only preparation, not a launch permission or an upstream health probe."""

import time

from .codex_config import NewawzConfig
from .codex_rpc import ProtocolError
from .database import CURRENT_SCHEMA_VERSION
from .execution_budget import shared_budget_view, shared_policy
from .execution_lock import execution_lock
from .management_lease import current_boot, observe_management
from .reception import blocking_reason
from .reply_scope import ReplyScope, queue_counts
from .reply_status import occupancy_reason
from .revisit_queue import enabled as revisits_enabled
from .schedule_policy import phase_at
from .schedules import schedule_snapshot
from .work_credentials import load_credential


def provider_blockers(profile, credential_file, model=None):
    try:
        config = NewawzConfig.load(profile)
        if model is not None:
            config = config.with_model(model)
    except (OSError, ValueError):
        return ["profile_invalid"]
    try:
        if credential_file is None:
            raise ValueError("An explicit credential file is required")
        load_credential(config, credential_file)
    except (OSError, ValueError):
        return ["credential_invalid"]
    return []


def inspect_preparation(connection, scope, now, seconds, boot):
    if connection.execute("PRAGMA user_version").fetchone()[0] != CURRENT_SCHEMA_VERSION:
        raise ValueError("Reply preparation requires the current schema")
    thread_id = scope.thread_id(connection)
    counts = queue_counts(connection, thread_id, now)
    policy, schedule = schedule_snapshot(connection)
    phase = phase_at(policy, schedule, now)[0] if policy else "unconfigured"
    if schedule["paused"]:
        phase = "paused"
    blockers = []
    if schedule["updated_at"] > now or counts.clock_invalid:
        blockers.append("clock_invalid")
    if phase != "working":
        blockers.append("schedule_" + phase)
    occupancy = occupancy_reason(connection, now)
    if occupancy:
        blockers.append(occupancy)
    common = shared_policy(connection)
    budget = shared_budget_view(connection, common, now, seconds) if common else None
    if budget is None:
        blockers.append("execution_policy_missing")
    elif budget["blocking_reason"]:
        blockers.append(budget["blocking_reason"])
    cursor = connection.execute("SELECT last_scan_at FROM wake_event_cursor WHERE id=1").fetchone()
    if cursor is None or (cursor[0] is not None and cursor[0] > now):
        blockers.append("event_clock_invalid")
    management = connection.execute("SELECT * FROM management_lease WHERE id=1").fetchone()
    automatic, reason, _ = observe_management(management, now, boot)
    if automatic == "enabled":
        blockers.append("management_busy")
    if counts.reserved_count and not occupancy:
        blockers.append("pending_dispatch_reconciliation")
    reception = blocking_reason(connection, scope.owner_id, "reply", now)
    if reception:
        blockers.append(reception)
    return {
        "observed_at": now,
        "phase": phase,
        "queue": counts.model_dump(),
        "budget": budget,
        "management": {"state": automatic, "reason": reason},
        "blockers": blockers,
    }


def check_reply_service(
    database,
    owner_id,
    profile,
    credential_file,
    *,
    seconds=3600,
    clock=time.time,
    model=None,
    native_home=None,
):
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ValueError("Reply timeout must be 1–3600 seconds")
    scope = ReplyScope(owner_id)
    blockers = provider_blockers(profile, credential_file, model)
    boot = current_boot()
    with execution_lock(database) as lease:
        if native_home is not None:
            blockers += native_blockers(
                database, owner_id, profile, native_home, model, clock, lease
            )
        with database.snapshot() as connection:
            report = inspect_preparation(connection, scope, int(clock()), seconds, boot)
            if native_home is None and revisits_enabled(connection, owner_id, int(clock())):
                blockers.append("native_revisit_receiver_required")
        lease.verify()
    report["blockers"] = blockers + report["blockers"]
    return {
        "ready": not report["blockers"],
        "execution_available": False,
        "automatic_execution_requested": False,
        "requested_timeout": seconds,
        **report,
    }


def native_blockers(database, owner_id, profile, directory, model, clock, lease):
    from .journal_capacity import can_admit
    from .native_messages import NativeMessageStore
    from .work_runtime import configured_native_runtime

    try:
        runtime = configured_native_runtime(
            database,
            directory,
            NewawzConfig.load(profile),
            model=model,
            clock=clock,
            read_only=True,
        )
        linked = runtime.journal.business_binding()
        runtime.check_session_binding(owner_id, linked.session_id, lease=lease)
        messages = NativeMessageStore(runtime.journal)
        messages.inspect("00000000-0000-0000-0000-000000000000")
        if runtime.journal.outstanding():
            return ["native_reconciliation_required"]
        if not can_admit(runtime.journal.capacity()) or not messages.capacity()["has_capacity"]:
            return ["native_storage_capacity"]
        return []
    except (OSError, ValueError, ProtocolError):
        return ["native_configuration_invalid"]

"""A short explicit stopped-writer window, using existing paired operations."""

import time

from .backup_process_evidence import observe_recorded
from .errors import ApiError
from .execution_lock import execution_lock
from .maintenance_backup import preflight_backup
from .maintenance_units import MaintenanceBusyError, MaintenanceSession, maintenance_lock
from .paired_backup import capture_generation


def _quiescent(database, home, policy):
    """No durable occupancy or unconfirmed recorded family may be waved away."""
    with execution_lock(database) as lease:
        with database.snapshot() as connection:
            active = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM runs WHERE status IN ('queued','running','stopping')) "
                "OR EXISTS(SELECT 1 FROM execution_windows WHERE state!='closed') "
                "OR EXISTS(SELECT 1 FROM work_main_creations WHERE state!='closed')"
            ).fetchone()[0]
        if active:
            return False
        observed = observe_recorded(home, budget=policy.budget_bytes)
        lease.verify()
    return all(item.get("absent_observed") is True for item in observed["observations"])


def _initial_report(clock):
    now = int(clock())
    return {
        "timestamp": now,
        "last_attempt_at": now,
        "last_success_at": None,
        "last_outcome": "deferred",
        "snapshot_id": None,
        "generation_id": None,
        "status": "deferred",
        "reason": "writers_unconfirmed",
        "quiescence_verified": False,
        "automatic_resume": False,
    }


def capture_for_maintenance(
    database,
    home,
    workspace,
    output,
    policy,
    *,
    units,
    unit_runner,
    writers_stopped=False,
    attachment_store=None,
    public_asset_store=None,
    reply_directory=None,
    state_file=None,
    clock=time.time,
):
    """Only an explicitly authorized call may pause entries; no remote work follows."""
    report = _initial_report(clock)
    if writers_stopped is not True:
        return report
    session = None
    options = {
        "attachment_store": attachment_store,
        "public_asset_store": public_asset_store,
        "reply_directory": reply_directory,
    }
    try:
        with maintenance_lock(units):
            preflight = preflight_backup(database, home, workspace, output, policy, **options)
            report["preflight"] = preflight
            if not preflight["ready"]:
                report["reason"] = preflight["reason"]
                return report
            session = MaintenanceSession(
                units,
                unit_runner,
                state_file or output.with_name(output.name + ".maintenance.json"),
                clock=clock,
            )
            try:
                for unit in units.intake_timers:
                    session.pause(unit)
                try:
                    session.workers_idle()
                    idle = _quiescent(database, home, policy)
                except (ApiError, ValueError):
                    idle = False
                if not idle:
                    report["reason"] = "execution_busy_or_unknown"
                    return report
                session.pause(units.api)
                # Check once more after ingress stopped. Core capture acquires the
                # same execution lease again and checks both ends of all sources.
                if not _quiescent(database, home, policy):
                    report["reason"] = "execution_changed"
                    return report
                captured = capture_generation(
                    database,
                    home,
                    workspace,
                    output,
                    writers_stopped=True,
                    **policy.paired_options(),
                    **options,
                )
                report.update(
                    status="captured",
                    reason=None,
                    last_outcome="succeeded",
                    last_success_at=int(clock()),
                    generation_id=captured["generation"],
                    capture=captured,
                )
            finally:
                restored = session.restore()
                report["entries"] = restored
                if restored["restore_pending"]:
                    report.update(
                        status="failed",
                        reason="entry_restore_incomplete",
                        last_outcome="failed",
                    )
    except MaintenanceBusyError:
        report["reason"] = "maintenance_busy"
    except ApiError as error:
        report["reason"] = "execution_busy" if error.code == "worker_busy" else "preflight_failed"
        if error.code != "worker_busy":
            report.update(status="failed", last_outcome="failed")
    except Exception as error:
        report.update(status="failed", last_outcome="failed")
        report["reason"] = "maintenance_failed"
        report["error_type"] = type(error).__name__
    return report

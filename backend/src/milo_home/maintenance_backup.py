"""Bounded local paired preflight, offline rehearsal and daily task evidence."""

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .database_identity import private_run_directory
from .execution_lock import execution_lock
from .maintenance_policy import BackupPolicy, trusted_bytes
from .native_session_backup import independent_output
from .paired_attachment_backup import attachment_index
from .paired_backup import rehearse_generation
from .paired_backup_state import source_state
from .public_asset_backup import public_asset_index
from .workspace_archive_files import RESERVE
from .workspace_git import names


def unclassified_files(workspace, policy):
    """Private names for an operator decision, never an automatic include rule."""
    others = set(names(workspace, "--others", "--exclude-standard"))
    return sorted(others - set(policy.include) - set(policy.exclude))


def preflight_backup(
    database,
    home: Path,
    workspace: Path,
    output: Path,
    policy: BackupPolicy,
    *,
    attachment_store=None,
    public_asset_store=None,
    reply_directory=None,
):
    """Inspect without creating an archive; capture repeats the same source checks."""
    for source in (database.path.parent, home, workspace):
        independent_output(source, output)
    if reply_directory is not None:
        independent_output(reply_directory, output)
    if attachment_store is not None:
        independent_output(attachment_store.root, output)
        attachment_store.check_workspace(workspace)
    if public_asset_store is not None:
        independent_output(public_asset_store.root, output)
        independent_output(public_asset_store.root, home)
        public_asset_store.check_workspace(workspace)
        if attachment_store is not None:
            independent_output(attachment_store.root, public_asset_store.root)
    private_run_directory(output.parent)
    if output.exists() or output.is_symlink() or output.resolve() != output:
        raise ValueError("Maintenance output requires a new canonical directory")
    unknown = unclassified_files(workspace, policy)
    if unknown:
        return {
            "ready": False,
            "reason": "classification_required",
            "unclassified": unknown,
            "budget_bytes": policy.budget_bytes,
            "policy_sha256": policy.sha256,
        }
    with execution_lock(database) as lease:
        state = source_state(
            database,
            home,
            workspace,
            lease,
            **policy.paired_options(),
            reply_directory=reply_directory,
        )
        attachments = attachment_index(database, policy.budget_bytes)
        if attachments and attachment_store is None:
            raise ValueError("Maintenance requires the indexed attachment store")
        assets = public_asset_index(database, policy.budget_bytes)
        if assets and public_asset_store is None:
            raise ValueError("Maintenance requires the indexed public asset store")
        required = state["estimated_peak_bytes"] + 4 * sum(
            r["size_bytes"] for r in [*attachments, *assets]
        )
        free = shutil.disk_usage(output.parent).free
        lease.verify()
    reason = None
    if required > policy.budget_bytes:
        reason = "budget_insufficient"
    elif free < required + RESERVE:
        reason = "disk_insufficient"
    return {
        "ready": reason is None,
        "reason": reason,
        "budget_bytes": policy.budget_bytes,
        "estimated_peak_bytes": required,
        "free_bytes": free,
        "reserve_bytes": RESERVE,
        "policy_sha256": policy.sha256,
    }


def rehearse_for_maintenance(archive, output, digest, policy: BackupPolicy):
    """No live database, service, model, network or execution binding is used."""
    return rehearse_generation(archive, output, digest, budget=policy.budget_bytes)


def _time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=UTC)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    result = int(parsed.timestamp())
    return result if result > 0 else None


def read_daily_backup_evidence(
    *,
    unit_runner,
    success_file: Path | None,
    now: datetime,
    unit=None,
):
    """Observe only systemd properties and a marker, never logs, config or remote data."""
    if now.tzinfo is None:
        raise ValueError("Maintenance observation time must include a timezone")
    timestamp = int(now.timestamp())
    evidence = {
        "timestamp": timestamp,
        "last_attempt_at": None,
        "last_success_at": None,
        "last_outcome": "unknown",
        "snapshot_id": None,
        "generation_id": None,
        "diagnostics": {},
    }
    try:
        marker = (
            _time(trusted_bytes(success_file, limit=128).decode("ascii").strip())
            if success_file is not None
            else None
        )
        if marker is not None and marker <= timestamp:
            evidence["last_success_at"] = marker
        else:
            evidence["diagnostics"]["marker"] = "invalid_or_future"
    except (OSError, ValueError, UnicodeError):
        evidence["diagnostics"]["marker"] = "unavailable"
    if unit is None:
        evidence["diagnostics"]["service"] = "not_configured"
        return evidence
    try:
        values = unit_runner("show", unit)
        started = _time(values.get("ExecMainStartTimestamp"))
        finished = _time(values.get("ExecMainExitTimestamp"))
        if started is None or started > timestamp or values.get("LoadState") != "loaded":
            evidence["diagnostics"]["service"] = "attempt_unavailable"
            return evidence
        evidence["last_attempt_at"] = started
        result = values.get("Result")
        inactive = values.get("ActiveState") in {"inactive", "failed"}
        if not inactive or finished is None or not started <= finished <= timestamp:
            evidence["diagnostics"]["service"] = "attempt_incomplete"
        elif result == "success" and values.get("ExecMainStatus") == "0":
            # A service result does not authenticate an exact remote snapshot.
            evidence["last_outcome"] = "succeeded"
        elif result not in {None, "", "success"} or values.get("ExecMainStatus") not in {None, "0"}:
            evidence["last_outcome"] = "failed"
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
        evidence["diagnostics"]["service"] = "unavailable"
    return evidence

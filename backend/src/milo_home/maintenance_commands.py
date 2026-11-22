"""Coordinate local maintenance through the same components as existing tools."""

import json
import os
import signal
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from .codex_journal import PrivateJournal
from .config import Settings
from .database import Database
from .database_identity import ServiceDatabase, private_run_directory
from .maintenance_backup import preflight_backup, rehearse_for_maintenance
from .maintenance_cache import checked_parent, publish_summary, read_summary
from .maintenance_capture import capture_for_maintenance
from .maintenance_evidence import load_facts, record_fact
from .maintenance_models import MaintenanceDocument
from .maintenance_policy import load_backup_policy, trusted_bytes
from .maintenance_retention import preview, run
from .maintenance_summary import collect_summary
from .maintenance_units import MaintenanceUnits, restore_maintenance_entries, systemd_unit_runner
from .public_asset_store import PublicAssetStore
from .work_attachment_store import AttachmentStore
from .work_runtime import WorkRuntime


def context(args):
    settings = Settings.from_env()
    database = (
        ServiceDatabase(settings.database, args.database_user)
        if args.database_user
        else Database(settings.database)
    )
    if args.run_directory is None:
        raise ValueError("Maintenance observation requires the exact existing runtime")
    home = private_run_directory(args.run_directory)
    journal = PrivateJournal(home, initialize=False, read_only=True)
    if args.maintenance_action == "run":
        journal.read_only = False
    attachments = settings.work_attachments
    store = (
        AttachmentStore(
            attachments.root, max_bytes=attachments.max_bytes, owner=getattr(database, "uid", None)
        )
        if attachments
        else None
    )
    public = settings.public_assets
    public_store = (
        PublicAssetStore(
            public.root, max_bytes=public.max_bytes, owner=getattr(database, "uid", None)
        )
        if public
        else None
    )
    return SimpleNamespace(
        settings=settings,
        database=database,
        home=home,
        runtime=WorkRuntime(database, journal),
        attachments=store,
        public_assets=public_store,
    )


def require_selection(case, workspace=None):
    selected = case.settings.maintenance
    linked = case.runtime.journal.business_binding()
    if selected is None or linked is None or linked.owner_id != str(selected.owner):
        raise ValueError("Maintenance requires the original selected owner")
    if workspace is not None and case.runtime.journal.binding().cwd != workspace:
        raise ValueError("Maintenance workspace differs from the registered main")
    checked_parent(selected.file)
    return selected


def cursor_file(selected):
    return selected.file.with_name("retention-cursor.json")


def read_cursor(selected):
    try:
        value = json.loads(trusted_bytes(cursor_file(selected), limit=256))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or set(value) != {"after"}:
        raise ValueError("Maintenance cursor must contain an explicit scan position")
    return value["after"]


def save_cursor(selected, value):
    path = cursor_file(selected)
    descriptor, temporary = tempfile.mkstemp(prefix=".cursor-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"after": value}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def observe(case, args):
    selected = require_selection(case, args.workspace)
    result = None
    if args.maintenance_action == "run":
        result = run(
            case.runtime, reply_directory=args.reply_directory, after=read_cursor(selected)
        )
        if result["status"] in {"maintained", "no_eligible_candidates", "below_threshold"}:
            save_cursor(selected, result["next_after"])
    summary = collect_summary(
        case.database,
        case.runtime,
        workspace=args.workspace,
        unit_runner=systemd_unit_runner,
        success_file=args.success_file,
        daily_unit=args.daily_unit,
        attachment_store=case.attachments,
        public_asset_store=case.public_assets,
        backup_directory=args.backup_directory,
        development_directory=args.development_directory,
        backup_facts=load_facts(selected, case.database),
    )
    identity = case.database.path.stat()
    publish_summary(
        selected.file,
        MaintenanceDocument(
            owner_id=selected.owner,
            database_device=identity.st_dev,
            database_inode=identity.st_ino,
            summary=summary,
        ),
    )
    return {"summary": summary.model_dump(), "maintenance": result}


def backup_action(case, args):
    policy = load_backup_policy(args.policy)
    options = {
        "attachment_store": case.attachments,
        "public_asset_store": case.public_assets,
        "reply_directory": args.reply_directory,
    }
    if args.maintenance_action == "backup-check":
        return preflight_backup(
            case.database, case.home, args.workspace, args.out, policy, **options
        )
    selected = require_selection(case, args.workspace)
    previous = signal.getsignal(signal.SIGTERM)

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    started = int(time.time())
    try:
        report = capture_for_maintenance(
            case.database,
            case.home,
            args.workspace,
            args.out,
            policy,
            units=MaintenanceUnits(),
            unit_runner=systemd_unit_runner,
            writers_stopped=args.writers_stopped,
            **options,
        )
    except KeyboardInterrupt:
        report = {
            "status": "failed",
            "last_outcome": "failed",
            "last_attempt_at": started,
            "reason": "interrupted",
        }
    finally:
        signal.signal(signal.SIGTERM, previous)
    record_fact(selected, case.database, "paired", report)
    return report


def rehearse(args):
    policy = load_backup_policy(args.policy)
    report = rehearse_for_maintenance(args.archive, args.out, args.sha256, policy)
    if args.record:
        settings = Settings.from_env()
        if settings.maintenance is None:
            raise ValueError("Recording recovery requires an explicit maintenance selection")
        manifest = json.loads(
            trusted_bytes(args.archive / "manifest.json", limit=policy.budget_bytes)
        )
        binding = manifest["baseline"]["binding"]
        identity = settings.database.stat()
        if (
            binding["database"],
            binding["database_device"],
            binding["database_inode"],
            binding["owner_id"],
        ) != (
            str(settings.database),
            identity.st_dev,
            identity.st_ino,
            str(settings.maintenance.owner),
        ):
            raise ValueError("Restored generation belongs to another maintenance source")
        now = int(time.time())
        record_fact(
            settings.maintenance,
            Database(settings.database),
            "restoration",
            {
                "last_attempt_at": now,
                "last_success_at": now,
                "last_outcome": "succeeded",
                "generation_id": report["generation"],
            },
        )
    return report


def execute(args):
    action = args.maintenance_action
    if action == "rehearse":
        report = rehearse(args)
    elif action == "restore-entries":
        report = restore_maintenance_entries(
            args.state_file, units=MaintenanceUnits(), unit_runner=systemd_unit_runner
        )
    elif action == "show":
        settings = Settings.from_env()
        if settings.maintenance is None:
            raise ValueError("Maintenance summary has not been configured")
        report = read_summary(
            settings.maintenance, settings.database, now=int(time.time())
        ).model_dump()
    else:
        case = context(args)
        if action in {"collect", "run"}:
            report = observe(case, args)
        elif action == "retention-preview":
            report = preview(case.runtime, reply_directory=args.reply_directory, after=args.after)
        else:
            report = backup_action(case, args)
    print(json.dumps(report, ensure_ascii=False))
    if (
        report.get("status") in {"failed", "blocked"}
        or report.get("restore_pending")
        or report.get("ready") is False
    ):
        raise SystemExit(1)
    if action == "run" and report["maintenance"]["status"] == "blocked":
        raise SystemExit(1)

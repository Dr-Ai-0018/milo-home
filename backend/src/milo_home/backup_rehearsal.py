"""Rehearse backup reopening and migration in a new private file only."""

from pathlib import Path

from .database import CURRENT_SCHEMA_VERSION, RECORD_TABLES, Database
from .database_inspection import inspect_database, readonly_snapshot
from .path_policy import PathPolicy


def record_counts(connection) -> dict:
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return {
        name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        for name in RECORD_TABLES
        if name in tables
    }


def rehearse_backup(source: Path, destination: Path) -> dict:
    PathPolicy.configured().require_external(destination)
    if not destination.is_absolute() or destination.parent.stat().st_mode & 0o077:
        raise ValueError("Rehearsal output requires a project-external private directory")
    if destination.resolve() == source.resolve():
        raise ValueError("Rehearsal must use an independent new file")
    # Refuse unknown/empty schemas before writing the destination, without migrating the source.
    with readonly_snapshot(source) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if not 1 <= version <= CURRENT_SCHEMA_VERSION:
            raise ValueError("Rehearsal source has an unsupported schema")
    Database(source).backup(destination)
    # All later work targets the copied snapshot, even if the source is still accepting writes.
    with readonly_snapshot(destination) as connection:
        copied_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if not 1 <= copied_version <= CURRENT_SCHEMA_VERSION:
            raise ValueError("Copied snapshot has an unsupported schema")
        before = record_counts(connection)
    restored = Database(destination)
    restored.initialize()
    inspection = inspect_database(destination)
    if not inspection["database_ok"]:
        raise ValueError("Rehearsed database failed inspection")
    with readonly_snapshot(destination) as connection:
        after = record_counts(connection)
    if any(after.get(name) != count for name, count in before.items()):
        raise ValueError("Rehearsal did not preserve existing record counts")
    return {
        "rehearsal_only": True,
        "source_migrated": False,
        "copied_schema_version": copied_version,
        "schema_version": inspection["schema_version"],
        "record_counts": after,
        "active_runs_retained": len(inspection["active_runs"]),
        "active_work_windows_retained": len(inspection["active_work_windows"]),
        "active_main_creations_retained": len(inspection["active_main_creations"]),
        "private_runtime_required": inspection["private_runtime_required"],
        "blocked_messages": inspection["blocked_messages"],
        "attention": inspection["attention"],
        "automatic_wakeup": False,
    }

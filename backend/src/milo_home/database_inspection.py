"""Local diagnostics without migration, message discovery, or process management."""

import time
from pathlib import Path

from .database import CURRENT_SCHEMA_VERSION, RECORD_TABLES
from .public_notes_integrity import public_notes_constraints_ok
from .public_works_integrity import public_works_constraints_ok
from .schedules import schedule_snapshot
from .sqlite_readonly import readonly_snapshot

CONVERSATION_TRIGGERS = {
    "threads_default_conversation",
    "conversations_identity_immutable",
    "messages_conversation_insert",
    "messages_conversation_update",
    "messages_default_conversation",
    "runs_conversation_insert",
    "runs_conversation_update",
    "runs_default_conversation",
    "run_messages_conversation_insert",
    "run_messages_conversation_update",
}
KNOCK_TRIGGERS = {"run_knocks_scope_insert", "run_knocks_immutable"}


def knock_constraints_ok(connection) -> bool:
    triggers = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    if not KNOCK_TRIGGERS.issubset(triggers):
        return False
    return (
        connection.execute(
            "SELECT 1 FROM run_knocks JOIN runs ON runs.id = run_knocks.run_id "
            "JOIN knocks ON knocks.id = run_knocks.knock_id "
            "WHERE knocks.disposition != 'accepted' "
            "OR knocks.conversation_id IS NOT runs.conversation_id "
            "OR knocks.received_at > runs.created_at LIMIT 1"
        ).fetchone()
        is None
    )


def conversation_constraints_ok(connection) -> bool:
    triggers = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    if not CONVERSATION_TRIGGERS.issubset(triggers):
        return False
    invalid = connection.execute(
        "SELECT 1 FROM messages "
        "LEFT JOIN conversations ON conversations.id = messages.conversation_id "
        "WHERE conversations.thread_id IS NOT messages.thread_id "
        "UNION ALL SELECT 1 FROM runs "
        "LEFT JOIN conversations ON conversations.id = runs.conversation_id "
        "WHERE conversations.thread_id IS NOT runs.thread_id "
        "UNION ALL SELECT 1 FROM run_messages JOIN runs ON runs.id = run_messages.run_id "
        "JOIN messages ON messages.id = run_messages.message_id "
        "WHERE runs.conversation_id IS NOT messages.conversation_id "
        "UNION ALL SELECT 1 FROM messages AS reply "
        "LEFT JOIN messages AS original ON original.id = reply.in_reply_to "
        "LEFT JOIN runs ON runs.id = reply.run_id WHERE reply.role = 'milo' "
        "AND (reply.conversation_id IS NOT original.conversation_id "
        "OR reply.conversation_id IS NOT runs.conversation_id) "
        "UNION ALL SELECT 1 FROM threads WHERE NOT EXISTS "
        "(SELECT 1 FROM conversations WHERE thread_id = threads.id AND is_default = 1) LIMIT 1"
    ).fetchone()
    return invalid is None


def work_recovery_snapshot(connection, now: int) -> dict:
    """Observe durable occupancy without inferring that a process is stopped."""
    windows = [
        dict(row)
        for row in connection.execute(
            "SELECT id,task_id,state,created_at,deadline_at,deadline_at <= ? "
            "AS deadline_exceeded FROM execution_windows WHERE state!='closed' ORDER BY id",
            (now,),
        )
    ]
    creations = [
        dict(row)
        for row in connection.execute(
            "SELECT id,session_id,state,created_at,deadline_at,deadline_at <= ? "
            "AS deadline_exceeded FROM work_main_creations WHERE state!='closed' ORDER BY id",
            (now,),
        )
    ]
    required = bool(
        connection.execute(
            "SELECT EXISTS (SELECT 1 FROM work_runtime_bindings) "
            "OR EXISTS (SELECT 1 FROM work_main_creations)"
        ).fetchone()[0]
    )
    return {
        "active_work_windows": windows,
        "active_main_creations": creations,
        "private_runtime_required": required,
    }


def inspect_constraints(connection, report, version):
    checks = [
        ("conversation", conversation_constraints_ok),
        ("knock", knock_constraints_ok),
    ]
    if version >= 34:
        checks.append(("public_notes", public_notes_constraints_ok))
    if version >= 35:
        checks.append(("public_works", public_works_constraints_ok))
    for name, check in checks:
        valid = check(connection)
        report[f"{name}_constraints_ok"] = valid
        if not valid:
            report["database_ok"] = False
            report["attention"].append(f"{name}_constraints_failed")
            return False
    return True


def inspect_database(path: Path, *, clock=time.time) -> dict:
    now = int(clock())
    with readonly_snapshot(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        integrity = all(row[0] == "ok" for row in connection.execute("PRAGMA quick_check"))
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchone() is None
        supported = 1 <= version <= CURRENT_SCHEMA_VERSION
        current = version == CURRENT_SCHEMA_VERSION
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        tables_ok = set(RECORD_TABLES).issubset(tables) if current else None
        report = {
            "observed_at": now,
            "schema_version": version,
            "expected_schema_version": CURRENT_SCHEMA_VERSION,
            "schema_supported": supported,
            "migration_required": supported and not current,
            "integrity_ok": integrity,
            "foreign_keys_ok": foreign_keys,
            "required_tables_ok": tables_ok,
            "database_ok": integrity and foreign_keys and current and tables_ok,
            "automatic_wakeup": False,
            "attention": [],
        }
        for condition, reason in [
            (not integrity, "integrity_failed"),
            (not foreign_keys, "foreign_keys_failed"),
            (not supported, "schema_unsupported"),
            (supported and not current, "schema_migration_required"),
            (current and not tables_ok, "schema_tables_missing"),
        ]:
            if condition:
                report["attention"].append(reason)
        if not report["database_ok"]:
            return report
        if not inspect_constraints(connection, report, version):
            return report
        policy, schedule = schedule_snapshot(connection)
        report["schedule"] = {
            "configured": policy is not None,
            "paused": bool(schedule["paused"]),
            "revision": schedule["revision"],
        }
        if policy is None:
            report["attention"].append("schedule_unconfigured")
        report["runs"] = [
            dict(row)
            for row in connection.execute(
                "SELECT status, count(*) AS count FROM runs GROUP BY status ORDER BY status"
            )
        ]
        report["active_runs"] = [
            dict(row)
            for row in connection.execute(
                "SELECT runs.id, runs.status, runs.created_at, runs.deadline_at, "
                "wake_dispatches.run_id IS NOT NULL AS automatic, "
                "runs.deadline_at <= ? AS deadline_exceeded FROM runs "
                "LEFT JOIN wake_dispatches ON wake_dispatches.run_id = runs.id "
                "WHERE runs.status IN ('queued', 'running', 'stopping')",
                (now,),
            )
        ]
        if report["active_runs"]:
            report["attention"].append("active_run_needs_observation")
        report.update(work_recovery_snapshot(connection, now))
        for key, reason in [
            ("active_work_windows", "active_work_window_needs_observation"),
            ("active_main_creations", "active_main_creation_needs_observation"),
            ("private_runtime_required", "private_work_runtime_required"),
        ]:
            if report[key]:
                report["attention"].append(reason)
        report["blocked_messages"] = connection.execute(
            "SELECT count(*) FROM wake_event_delivery JOIN wake_events "
            "ON wake_events.id = wake_event_delivery.event_id "
            "WHERE wake_events.kind = 'message' AND wake_events.state = 'pending' "
            "AND blocked_reason IS NOT NULL"
        ).fetchone()[0]
        if report["blocked_messages"]:
            report["attention"].append("automatic_messages_blocked")
        cursor = connection.execute(
            "SELECT last_scan_at FROM wake_event_cursor WHERE id = 1"
        ).fetchone()
        if cursor and cursor[0] is not None and cursor[0] > now:
            report["attention"].append("event_clock_ahead")
        return report

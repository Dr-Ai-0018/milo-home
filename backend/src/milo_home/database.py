import os
import sqlite3
from contextlib import closing, contextmanager
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

from .path_policy import PathPolicy
from .sqlite_readonly import readonly_snapshot

MIGRATIONS = (
    (1, "001_initial.sql"),
    (2, "002_manual_runs.sql"),
    (3, "003_schedule.sql"),
    (4, "004_wakeup_events.sql"),
    (5, "005_automatic_dispatch.sql"),
    (6, "006_conversations.sql"),
    (7, "007_run_activity.sql"),
    (8, "008_knocks.sql"),
    (9, "009_management_lease.sql"),
    (10, "010_run_knocks.sql"),
    (11, "011_work_intake.sql"),
    (12, "012_execution_occupancy.sql"),
    (13, "013_shared_execution_budget.sql"),
    (14, "014_work_start_intents.sql"),
    (15, "015_work_progress.sql"),
    (16, "016_work_output.sql"),
    (17, "017_work_reconciliation.sql"),
    (18, "018_main_creation.sql"),
    (19, "019_main_attempts.sql"),
    (20, "020_work_artifacts.sql"),
    (21, "021_work_deliverables.sql"),
    (22, "022_work_turn_supplements.sql"),
    (23, "023_work_receipt_tasks.sql"),
    (24, "024_work_attachments.sql"),
    (25, "025_automatic_services.sql"),
    (26, "026_reply_choices.sql"),
    (27, "027_reception.sql"),
    (28, "028_reception_admissions.sql"),
    (29, "029_revisit_claims.sql"),
    (30, "030_revisit_capability.sql"),
    (31, "031_work_decisions.sql"),
    (32, "032_work_choice_evidence.sql"),
    (33, "033_work_revisit_claims.sql"),
    (34, "034_public_notes.sql"),
    (35, "035_public_works.sql"),
)
CURRENT_SCHEMA_VERSION = MIGRATIONS[-1][0]
RECORD_TABLES = (
    "users",
    "invitations",
    "sessions",
    "auth_buckets",
    "threads",
    "messages",
    "runs",
    "run_messages",
    "schedule_state",
    "wake_events",
    "wake_event_cursor",
    "wake_dispatches",
    "wake_event_delivery",
    "wake_run_events",
    "conversations",
    "run_activity",
    "knocks",
    "management_lease",
    "run_knocks",
    "work_access",
    "workspaces",
    "work_sessions",
    "work_tasks",
    "work_inputs",
    "work_stops",
    "execution_windows",
    "execution_policy",
    "legacy_launches",
    "execution_permits",
    "work_runtime_bindings",
    "execution_start_intents",
    "work_results",
    "work_events",
    "work_reconciliations",
    "work_main_creations",
    "work_main_attempt_retirements",
    "work_main_attempt_grants",
    "work_artifacts",
    "work_deliverables",
    "work_steer_turns",
    "work_steer_inputs",
    "work_receipt_tasks",
    "work_attachment_batches",
    "work_attachments",
    "automatic_services",
    "reply_run_scopes",
    "reply_execution_results",
    "message_intake",
    "revisit_intents",
    "revisit_sources",
    "message_reply_links",
    "message_decisions",
    "reception_changes",
    "reception_slots",
    "reception_run_bases",
    "reception_returned_attempts",
    "revisit_claims",
    "revisit_claim_sources",
    "revisit_controls",
    "message_control_decisions",
    "reply_revisit_capabilities",
    "work_revisits",
    "work_decisions",
    "work_continuations",
    "work_revisit_inputs",
    "work_turn_bases",
    "work_choice_receipts",
    "work_result_receptions",
    "work_decision_capabilities",
    "work_revisit_claims",
    "work_window_facts",
    "public_notes",
    "public_note_revisions",
    "public_works",
    "public_work_revisions",
    "public_work_assets",
    "public_work_revision_assets",
)


class Database:
    def __init__(self, path: Path, *, paths: PathPolicy | None = None):
        self.path = path
        self.paths = paths if paths is not None else PathPolicy.configured()
        self.paths.require_external(path)

    @contextmanager
    def snapshot(self):
        with readonly_snapshot(self.path, policy=self.paths) as connection:
            yield connection

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.stat().st_mode & 0o077:
            raise ValueError("Database directory permissions must be private (0700)")
        if self.path.is_symlink():
            raise ValueError("Database must not be a symbolic link")
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        if self.path.stat().st_mode & 0o077:
            raise ValueError("Database permissions must be private (0600)")
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if not 0 <= version <= CURRENT_SCHEMA_VERSION:
                raise ValueError("Unsupported database schema version")
            if (
                version == 0
                and connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
            ):
                raise ValueError("Unversioned non-empty database is not supported")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                objects = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if objects:
                    raise ValueError("Unversioned non-empty database is not supported")
            for number, name in MIGRATIONS:
                if version >= number:
                    continue
                migration = files("milo_home").joinpath("migrations", name).read_text()
                statement = ""
                for line in migration.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise ValueError("Migration has an incomplete statement")
                connection.execute(f"PRAGMA user_version={number}")
            if not 0 <= version <= CURRENT_SCHEMA_VERSION:
                raise ValueError("Unsupported database schema version")
            connection.commit()

    @contextmanager
    def connect(self, *, write: bool = False):
        uri = f"file:{quote(str(self.path))}?mode=rw"
        connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def backup(self, destination: Path):
        if destination.resolve() == self.path.resolve():
            raise ValueError("Backup must be a new independent path")
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            uri = f"file:{quote(str(self.path))}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as source:
                with closing(sqlite3.connect(destination)) as target:
                    source.backup(target)
                    if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("Backup integrity check failed")
                    if target.execute("PRAGMA foreign_key_check").fetchall():
                        raise ValueError("Backup foreign key check failed")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

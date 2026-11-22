import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_tasks import work_case as work_case

from milo_home.codex_app_server import ThreadBinding
from milo_home.codex_config import CodexRuntime, NewawzConfig
from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.database import Database
from milo_home.database_identity import ServiceDatabase
from milo_home.errors import ApiError
from milo_home.execution_lock import ExecutionIdentityError, execution_lock
from milo_home.work_runtime import WorkRuntime

SYNTHETIC_CONFIG = b'[model_providers.synthetic-provider]\nbase_url="https://synthetic.invalid"\n'

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Shared host lease requires root")


def journal_at(directory, workspace, thread="synthetic-main"):
    directory.mkdir(mode=0o700)
    config = NewawzConfig(
        "synthetic-provider", "synthetic-model", "SYNTHETIC_KEY", SYNTHETIC_CONFIG
    )
    config.materialize(directory)
    journal = PrivateJournal(directory)
    journal.bind(
        ThreadBinding(thread, config.provider, config.model, workspace),
        config,
        CodexRuntime(Path("/synthetic/codex"), Path("/synthetic/host")),
    )
    return journal


@pytest.fixture
def runtime_case(execution_case, tmp_path):
    work, _, _, now = execution_case
    journal = journal_at(tmp_path / "runtime", tmp_path)
    runtime = WorkRuntime(work[0], journal, lambda: now[0])
    return execution_case, runtime, journal


def bind(runtime_case, lease):
    case, runtime, _ = runtime_case
    work = case[0]
    return runtime.bind(work[1], work[-1]["id"], lease=lease)


def test_initialization_can_check_database_identity_before_a_journal_exists(execution_case):
    database = execution_case[0][0]
    runtime = WorkRuntime(database, None)
    with execution_lock(database) as lease:
        path, device, inode = runtime._identity(lease)
    assert path == str(database.path.resolve())
    assert (device, inode) == (database.path.stat().st_dev, database.path.stat().st_ino)
    assert runtime.native_policy is None


def test_binding_and_exact_window_association_survive_reopen(runtime_case):
    case, runtime, journal = runtime_case
    work = case[0]
    task, window = reserve(case)
    with execution_lock(work[0]) as lease:
        linked = bind(runtime_case, lease)
        assert bind(runtime_case, lease) == linked
        intent = runtime.claim_start(work[1], window["id"], lease=lease)
    reopened = PrivateJournal(journal.path.parent)
    assert reopened.business_binding() == linked
    assert reopened.binding() == journal.binding()
    with reopened._connect() as connection:
        row = dict(connection.execute("SELECT * FROM stream_work").fetchone())
    assert row == {"stream": intent.stream_id, "window_id": window["id"], "task_id": task["id"]}
    assert intent.unit_name == window["unit_name"]
    assert intent.created_at == window["created_at"]
    assert intent.deadline_at == window["deadline_at"]
    assert reopened.outstanding()[0]["unit"] == intent.unit_name
    # The claim itself neither consumes a turn nor says the process started.
    with work[0].connect() as connection:
        assert connection.execute("SELECT count(*) FROM execution_permits").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM work_tasks").fetchone()[0] == "queued"
        assert connection.execute("SELECT state FROM execution_windows").fetchone()[0] == "held"


def test_recreated_coordinator_cannot_reclaim_even_a_closed_stream(runtime_case):
    case, runtime, journal = runtime_case
    work = case[0]
    _, window = reserve(case)
    with execution_lock(work[0]) as lease:
        bind(runtime_case, lease)
        intent = runtime.claim_start(work[1], window["id"], lease=lease)
        assert journal.close_stream(intent.stream_id, stop=lambda: True)
    recreated = WorkRuntime(
        Database(work[0].path), PrivateJournal(journal.path.parent), runtime.clock
    )
    with (
        execution_lock(work[0]) as lease,
        pytest.raises(ApiError, match="startup_already_recorded"),
    ):
        recreated.claim_start(work[1], window["id"], lease=lease)


@pytest.mark.parametrize("operation", ["bind", "claim"])
def test_missing_wrong_or_expired_host_lease_cannot_write(runtime_case, tmp_path, operation):
    case, runtime, _ = runtime_case
    work = case[0]
    _, window = reserve(case)
    with execution_lock(work[0]) as lease:
        bind(runtime_case, lease)
    callback = (
        (lambda: bind(runtime_case, lease))
        if operation == "bind"
        else (lambda: runtime.claim_start(work[1], window["id"], lease=lease))
    )
    with pytest.raises(ExecutionIdentityError):
        callback()
    other = Database(tmp_path / "other.sqlite3")
    other.initialize()
    with execution_lock(other) as lease, pytest.raises(ExecutionIdentityError, match="another"):
        callback()
    with work[0].connect() as connection:
        assert connection.execute("SELECT count(*) FROM execution_start_intents").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["owner", "root", "runtime", "thread"])
def test_binding_rejects_identity_workspace_or_private_main_changes(runtime_case, tmp_path, change):
    case, runtime, journal = runtime_case
    work = case[0]
    with execution_lock(work[0]) as lease:
        linked = bind(runtime_case, lease)
        if change == "owner":
            with pytest.raises(ApiError):
                runtime.bind(work[2], work[-1]["id"], lease=lease)
        elif change == "root":
            root = tmp_path / "moved"
            root.mkdir()
            work[4].register_workspace(work[3], "Moved", root)
            with pytest.raises(ProtocolError, match="workspace"):
                bind(runtime_case, lease)
        else:
            other = journal_at(
                tmp_path / "another-runtime",
                tmp_path,
                "another-thread" if change == "thread" else "synthetic-main",
            )
            runtime.journal = other
            with pytest.raises(ProtocolError, match="main changed"):
                bind(runtime_case, lease)
        assert journal.business_binding() == linked


def test_database_backup_requires_explicit_runtime_reconciliation(runtime_case, tmp_path):
    case, runtime, journal = runtime_case
    work = case[0]
    with execution_lock(work[0]) as lease:
        bind(runtime_case, lease)
    backup = tmp_path / "backup.sqlite3"
    work[0].backup(backup)
    restored = Database(backup)
    recreated = WorkRuntime(restored, journal, runtime.clock)
    with execution_lock(restored) as lease, pytest.raises(ProtocolError, match="another business"):
        recreated.bind(work[1], work[-1]["id"], lease=lease)


@pytest.mark.parametrize("change", ["revoke", "regrant", "pause", "deadline", "rewind", "stop"])
def test_claim_rechecks_current_authority_schedule_and_window(runtime_case, change):
    case, runtime, _ = runtime_case
    work, _, schedule, now = case
    task, window = reserve(case)
    with execution_lock(work[0]) as lease:
        bind(runtime_case, lease)
        if change in {"revoke", "regrant"}:
            work[4].revoke(work[1])
            if change == "regrant":
                work[4].grant(work[1])
        elif change == "pause":
            schedule.pause(True)
        elif change == "deadline":
            now[0] = window["deadline_at"]
        elif change == "rewind":
            now[0] = window["created_at"] - 1
        else:
            work[5].stop(work[1], task["id"], "synthetic-stop")
        with pytest.raises(ApiError):
            runtime.claim_start(work[1], window["id"], lease=lease)
    with work[0].connect() as connection:
        assert connection.execute("SELECT count(*) FROM execution_start_intents").fetchone()[0] == 0


def test_private_write_failure_keeps_durable_claim_and_unknown_occupancy(runtime_case, monkeypatch):
    case, runtime, journal = runtime_case
    work = case[0]
    _, window = reserve(case)
    with execution_lock(work[0]) as lease:
        bind(runtime_case, lease)

        def fail(*args, **kwargs):
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(journal, "open_work_stream", fail)
        with pytest.raises(OSError):
            runtime.claim_start(work[1], window["id"], lease=lease)
    with work[0].connect() as connection:
        assert connection.execute("SELECT count(*) FROM execution_start_intents").fetchone()[0] == 1
        assert connection.execute("SELECT state FROM execution_windows").fetchone()[0] == "unknown"
        assert (
            connection.execute("SELECT status FROM work_tasks").fetchone()[0]
            == "needs_reconciliation"
        )
    with execution_lock(work[0]) as lease, pytest.raises(ApiError):
        runtime.claim_start(work[1], window["id"], lease=lease)


def test_business_commit_precedes_private_stream_even_on_abrupt_exit(runtime_case, monkeypatch):
    case, runtime, journal = runtime_case
    work = case[0]
    _, window = reserve(case)

    class Crash(BaseException):
        pass

    def crash(*args, **kwargs):
        raise Crash()

    with execution_lock(work[0]) as lease:
        bind(runtime_case, lease)
        monkeypatch.setattr(journal, "open_work_stream", crash)
        with pytest.raises(Crash):
            runtime.claim_start(work[1], window["id"], lease=lease)
    assert not journal.outstanding()
    with (
        execution_lock(work[0]) as lease,
        pytest.raises(ApiError, match="startup_already_recorded"),
    ):
        runtime.claim_start(work[1], window["id"], lease=lease)


def test_business_binding_partial_commit_can_only_finish_the_same_main(runtime_case, monkeypatch):
    case, runtime, journal = runtime_case
    work = case[0]
    original = journal.bind_business

    def fail(*args):
        raise OSError("synthetic journal unavailable")

    with execution_lock(work[0]) as lease:
        monkeypatch.setattr(journal, "bind_business", fail)
        with pytest.raises(OSError):
            bind(runtime_case, lease)
        monkeypatch.setattr(journal, "bind_business", original)
        assert bind(runtime_case, lease) == journal.business_binding()


def test_private_association_cannot_be_rebound(runtime_case):
    case, _, journal = runtime_case
    with execution_lock(case[0][0]) as lease:
        linked = bind(runtime_case, lease)
        with pytest.raises(ProtocolError, match="binding changed"):
            journal.bind_business(replace(linked, session_id="different"))
        with pytest.raises(ProtocolError, match="private main"):
            journal.bind_business(replace(linked, fingerprint="0" * 64))


def test_sql_immutability_and_backups_preserve_claim(runtime_case, tmp_path):
    case, runtime, _ = runtime_case
    work = case[0]
    _, window = reserve(case)
    with execution_lock(work[0]) as lease:
        bind(runtime_case, lease)
        runtime.claim_start(work[1], window["id"], lease=lease)
    with work[0].connect(write=True) as connection:
        for table in ["work_runtime_bindings", "execution_start_intents"]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"DELETE FROM {table}")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE {table} SET created_at=created_at+1")
    backup = tmp_path / "claim-backup.sqlite3"
    work[0].backup(backup)
    with Database(backup).connect() as connection:
        assert connection.execute("SELECT count(*) FROM execution_start_intents").fetchone()[0] == 1


def test_runtime_v1_upgrade_preserves_existing_unknown_delivery(runtime_case):
    _, _, journal = runtime_case
    stream = journal.open_stream("milo-work-" + "1" * 32 + ".service")
    event = {"kind": "intent", "request": {"id": 1, "method": "turn/start", "params": {}}}
    journal.record(stream, event)
    with journal._connect() as connection:
        connection.execute("DROP TABLE stream_work")
        connection.execute("DROP TABLE business_binding")
        connection.execute("PRAGMA user_version=1")
    reopened = PrivateJournal(journal.path.parent)
    assert reopened.outstanding()[0]["delivery"] == "pending"
    assert reopened.binding() == journal.binding()
    with reopened._connect() as connection:
        assert json.loads(connection.execute("SELECT payload FROM entries").fetchone()[0]) == event
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_root_private_journal_is_never_opened_as_database_account(runtime_case, owned_database):
    case, runtime, journal = runtime_case
    work = case[0]
    _, window = reserve(case)
    path, account = owned_database
    copied = path.parent / "seeded.sqlite3"
    work[0].backup(copied)
    os.chown(copied, account.pw_uid, account.pw_gid)
    os.replace(copied, path)
    database = ServiceDatabase(path, account.pw_name)
    runtime = WorkRuntime(database, journal, runtime.clock)
    with execution_lock(database) as lease:
        runtime.bind(work[1], work[-1]["id"], lease=lease)
        runtime.claim_start(work[1], window["id"], lease=lease)
    assert os.geteuid() == 0
    assert all(p.stat().st_uid == account.pw_uid for p in path.parent.iterdir())
    assert all(p.stat().st_uid == 0 for p in journal.path.parent.iterdir())


def test_v13_upgrade_preserves_work_history_policy_and_reserved_window(runtime_case):
    from test_work_migration import remove_reconciliation_schema, snapshot

    case, _, _ = runtime_case
    database = case[0][0]
    reserve(case)
    with database.connect(write=True) as connection:
        remove_reconciliation_schema(connection)
        # Reconstruct the actual v13 schema, including removal of later triggers.
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND "
            "(tbl_name IN ('work_events','work_results') OR name IN "
            "('work_task_created_event','work_task_status_event',"
            "'work_input_created_event','work_input_delivery_event'))"
        ).fetchall()
        for row in triggers:
            connection.execute(f'DROP TRIGGER "{row[0]}"')
        connection.execute("DROP TABLE work_events")
        connection.execute("DROP TABLE work_results")
        connection.execute("DROP TABLE execution_start_intents")
        connection.execute("DROP TABLE work_runtime_bindings")
        connection.execute("PRAGMA user_version=13")
    before = snapshot(database)
    database.initialize()
    after = snapshot(database)
    for table, rows in before.items():
        assert after[table] == rows
    assert after["execution_start_intents"] == after["work_runtime_bindings"] == []
    with database.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_competing_claims_have_one_winner_and_one_private_stream(runtime_case):
    case, runtime, journal = runtime_case
    work = case[0]
    _, window = reserve(case)
    with execution_lock(work[0]) as lease:
        bind(runtime_case, lease)

        def attempt(_):
            try:
                return runtime.claim_start(work[1], window["id"], lease=lease)
            except ApiError as error:
                assert error.code == "startup_already_recorded"
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, [1, 2]))
    assert sum(item is not None for item in outcomes) == 1
    assert len(journal.outstanding()) == 1


def test_business_write_failure_does_not_leave_a_private_stream(runtime_case):
    case, runtime, journal = runtime_case
    work = case[0]
    _, window = reserve(case)
    with execution_lock(work[0]) as lease:
        bind(runtime_case, lease)
        with work[0].connect(write=True) as connection:
            connection.execute(
                "CREATE TRIGGER synthetic_failure BEFORE INSERT ON execution_start_intents "
                "BEGIN SELECT RAISE(ABORT,'synthetic'); END;"
            )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic"):
            runtime.claim_start(work[1], window["id"], lease=lease)
    assert not journal.outstanding()
    with work[0].connect() as connection:
        assert connection.execute("SELECT count(*) FROM execution_start_intents").fetchone()[0] == 0


def test_process_death_between_business_commit_and_private_write_cannot_reclaim(runtime_case):
    case, runtime, journal = runtime_case
    work = case[0]
    _, window = reserve(case)
    with execution_lock(work[0]) as lease:
        bind(runtime_case, lease)
    program = """
import os,sys
from pathlib import Path
from milo_home.database import Database
from milo_home.codex_journal import PrivateJournal
from milo_home.execution_lock import execution_lock
from milo_home.work_runtime import WorkRuntime
database = Database(Path(sys.argv[1]))
journal = PrivateJournal(Path(sys.argv[2]))
journal.open_work_stream = lambda *args, **kwargs: os._exit(92)
runtime = WorkRuntime(database, journal, lambda: int(sys.argv[5]))
with execution_lock(database) as lease:
    runtime.claim_start(sys.argv[3], sys.argv[4], lease=lease)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(work[0].path),
            str(journal.path.parent),
            work[1],
            window["id"],
            str(int(runtime.clock())),
        ],
        timeout=5,
    )
    assert result.returncode == 92
    assert not PrivateJournal(journal.path.parent).outstanding()
    with (
        execution_lock(work[0]) as lease,
        pytest.raises(ApiError, match="startup_already_recorded"),
    ):
        runtime.claim_start(work[1], window["id"], lease=lease)
    with work[0].connect() as connection:
        assert connection.execute("SELECT count(*) FROM execution_start_intents").fetchone()[0] == 1


def test_private_stream_scope_failure_is_atomic(runtime_case):
    case, runtime, journal = runtime_case
    work = case[0]
    _, window = reserve(case)
    with execution_lock(work[0]) as lease:
        binding = bind(runtime_case, lease)
        with pytest.raises(ProtocolError, match="does not match"):
            journal.open_work_stream(
                replace(binding, owner_id="different"),
                unit=window["unit_name"],
                stream="a" * 32,
                window_id=window["id"],
                task_id=window["task_id"],
            )
        assert not journal.outstanding()
        intent = runtime.claim_start(work[1], window["id"], lease=lease)
        assert journal.close_stream(intent.stream_id, stop=lambda: True)
        with pytest.raises(sqlite3.IntegrityError):
            journal.open_work_stream(
                binding,
                unit="milo-work-" + "b" * 32 + ".service",
                stream="b" * 32,
                window_id=window["id"],
                task_id=window["task_id"],
            )
    assert not journal.outstanding()


def test_failed_runtime_v2_migration_preserves_v1_unknown_evidence(runtime_case, monkeypatch):
    from milo_home import codex_journal as module

    _, _, journal = runtime_case
    stream = journal.open_stream("milo-work-" + "c" * 32 + ".service")
    journal.record(
        stream, {"kind": "intent", "request": {"id": 1, "method": "turn/start", "params": {}}}
    )
    with journal._connect() as connection:
        connection.execute("DROP TABLE stream_work")
        connection.execute("DROP TABLE business_binding")
        connection.execute("PRAGMA user_version=1")
    with monkeypatch.context() as patch:
        patch.setattr(
            module, "BUSINESS_SCHEMA", (module.BUSINESS_SCHEMA[0], "INSERT INTO missing VALUES (1)")
        )
        with pytest.raises(sqlite3.OperationalError):
            PrivateJournal(journal.path.parent)
    with journal._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='business_binding'"
            ).fetchone()
            is None
        )
    reopened = PrivateJournal(journal.path.parent)
    assert reopened.outstanding()[0]["delivery"] == "pending"

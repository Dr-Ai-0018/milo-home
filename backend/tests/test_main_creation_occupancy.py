import sqlite3
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_execution_control import legacy, reserve
from test_work_migration import remove_main_creation_schema, snapshot
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.database import Database
from milo_home.dispatch import DispatchStore
from milo_home.errors import ApiError
from milo_home.execution_control import execution_busy


def claim(connection, session, state="held"):
    identity = str(uuid4())
    connection.execute(
        "INSERT INTO work_main_creations (id,session_id,choice_digest,owner_revision,"
        "workspace_revision,created_at,deadline_at,state) VALUES (?,?,?,1,1,1,61,?)",
        (identity, session, "a" * 64, state),
    )
    return identity


def test_populated_v17_upgrade_preserves_rows_and_does_not_create_a_main(execution_case, tmp_path):
    work = execution_case[0]
    reserve(execution_case)
    with work[0].connect(write=True) as connection:
        remove_main_creation_schema(connection)
        connection.execute("PRAGMA user_version=17")
    before = snapshot(work[0])
    work[0].initialize()
    after = snapshot(work[0])
    added = {
        "work_main_creations",
        "work_main_attempt_retirements",
        "work_main_attempt_grants",
        "work_artifacts",
        "work_deliverables",
    }
    assert set(after) - set(before) == added
    assert all(after[name] == [] for name in added)
    assert all(after[name] == rows for name, rows in before.items())
    backup = tmp_path / "upgraded.sqlite3"
    work[0].backup(backup)
    restored = Database(backup)
    restored.initialize()
    assert snapshot(restored) == after
    with restored.connect() as connection:
        assert execution_busy(connection)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("state", ["held", "unknown"])
def test_creation_blocks_both_queue_services_and_raw_sql_across_backup(
    execution_case, tmp_path, state
):
    work, control, _, _ = execution_case
    thread, runs = legacy(work[0], work[1])
    old = runs.queue(thread, 60)
    runs.finish_failed(old, "execution_failed")
    task = create(work)
    with work[0].connect(write=True) as connection:
        claim(connection, work[-1]["id"], state)
        assert execution_busy(connection)
    with pytest.raises(ApiError, match="run_busy"):
        control.reserve(work[1], task["id"], 60)
    with pytest.raises(ApiError, match="run_busy"):
        runs.queue(thread, 60)
    assert DispatchStore(work[0]).reserve() is None
    with work[0].connect(write=True) as connection:
        for sql, args in [
            ("UPDATE runs SET status='queued' WHERE id=?", (old,)),
            (
                "INSERT INTO runs (id,thread_id,status,created_at) VALUES (?,?,'queued',1)",
                (str(uuid4()), thread),
            ),
            (
                "INSERT INTO execution_windows VALUES (?,?,?,1,61,'held',NULL,NULL)",
                (str(uuid4()), task["id"], "synthetic.service"),
            ),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match="main creation"):
                connection.execute(sql, args)
    backup = tmp_path / "occupied.sqlite3"
    work[0].backup(backup)
    restored = Database(backup)
    restored.initialize()
    with restored.connect() as connection:
        assert execution_busy(connection)
        assert connection.execute("SELECT state FROM work_main_creations").fetchone()[0] == state


@pytest.mark.parametrize("occupied", ["legacy", "work"])
def test_existing_execution_blocks_creation_at_sql_level(execution_case, occupied):
    work = execution_case[0]
    if occupied == "work":
        reserve(execution_case)
    else:
        thread, runs = legacy(work[0], work[1])
        runs.queue(thread, 60)
    with work[0].connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="execution already"):
            claim(connection, work[-1]["id"])


def test_creation_identity_and_terminal_occupancy_cannot_be_rewritten(execution_case):
    work = execution_case[0]
    with work[0].connect(write=True) as connection:
        identity = claim(connection, work[-1]["id"])
        for mutation in [
            "UPDATE work_main_creations SET deadline_at=62",
            "UPDATE work_main_creations SET choice_digest='replacement'",
            "DELETE FROM work_main_creations",
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(mutation)
        connection.execute("UPDATE work_main_creations SET state='closed' WHERE id=?", (identity,))
        assert not execution_busy(connection)
        with pytest.raises(sqlite3.IntegrityError, match="cannot reopen"):
            connection.execute("UPDATE work_main_creations SET state='held'")


def test_closed_work_window_cannot_be_reactivated_during_main_creation(execution_case):
    work, control, _, _ = execution_case
    _, window = reserve(execution_case)
    assert control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="completed")
    with work[0].connect(write=True) as connection:
        claim(connection, work[-1]["id"])
        with pytest.raises(sqlite3.IntegrityError, match="main creation"):
            connection.execute(
                "UPDATE execution_windows SET state='held',ended_at=NULL WHERE id=?",
                (window["id"],),
            )

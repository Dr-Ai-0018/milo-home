import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.database import Database
from milo_home.errors import ApiError
from milo_home.work_progress import WorkProgress


def test_events_are_atomic_ordered_facts_and_retry_does_not_duplicate(work_case):
    database, owner, _, _, _, tasks, session = work_case
    key = str(uuid4())
    task = create(work_case, text="private-body /root/secret SYNTHETIC_KEY", key=key)
    progress = WorkProgress(database)
    first = progress.events(owner, task["id"], limit=1)
    assert first["has_more"] and first["next_after"] == 1
    assert first["events"][0]["kind"] == "task_status"
    second = progress.events(owner, task["id"], after=1, limit=1)
    assert second["events"][0]["input_id"] == task["initial_input"]["id"]
    assert not second["has_more"] and second["next_after"] == 2
    tasks.create(owner, session["id"], task["initial_input"]["body"], key)
    assert progress.events(owner, task["id"])["next_after"] == 2
    assert progress.results(owner, task["id"]) == {"results": []}
    tasks.stop(owner, task["id"], str(uuid4()))
    page = progress.events(owner, task["id"], after=2)
    assert [(e["kind"], e["state"]) for e in page["events"]] == [
        ("task_status", "cancelled"),
        ("input_delivery", "cancelled"),
    ]
    serialized = str(progress.events(owner, task["id"]))
    for forbidden in ["private-body", "/root", "SYNTHETIC_KEY", "client_key", "thread_id"]:
        assert forbidden not in serialized
    assert progress.events(owner, task["id"], after=4) == {
        "events": [],
        "next_after": 4,
        "has_more": False,
    }
    with pytest.raises(ApiError, match="work_cursor_ahead"):
        progress.events(owner, task["id"], after=5)


def test_events_and_results_recheck_ownership_and_authority_generation(work_case):
    database, owner, other, _, access, _, _ = work_case
    task = create(work_case)
    progress = WorkProgress(database)
    for read in [progress.events, progress.results]:
        with pytest.raises(ApiError, match="work_not_found"):
            read(other, task["id"])
    access.revoke(owner)
    access.grant(owner)
    for read in [progress.events, progress.results]:
        with pytest.raises(ApiError, match="work_not_found"):
            read(owner, task["id"])


def test_concurrent_inputs_have_one_gapless_cursor_and_events_survive_backup(work_case, tmp_path):
    database, owner, _, _, _, tasks, _ = work_case
    task = create(work_case)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: tasks.append(owner, task["id"], "补充", str(uuid4())), range(8)))
    before = WorkProgress(database).events(owner, task["id"])
    assert [row["sequence"] for row in before["events"]] == list(range(1, 11))
    backup = tmp_path / "progress-backup.sqlite3"
    database.backup(backup)
    assert WorkProgress(Database(backup)).events(owner, task["id"]) == before


def test_event_failure_rolls_back_business_transition_and_events_are_immutable(work_case):
    database, owner, _, _, _, tasks, _ = work_case
    task = create(work_case)
    with database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_event BEFORE INSERT ON work_events "
            "WHEN NEW.state='cancelled' BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic"):
        tasks.stop(owner, task["id"], str(uuid4()))
    assert tasks.get(owner, task["id"])["status"] == "queued"
    assert WorkProgress(database).events(owner, task["id"])["next_after"] == 2
    with database.connect(write=True) as connection:
        for statement in ["DELETE FROM work_events", "UPDATE work_events SET sequence=9"]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)


def test_v14_upgrade_preserves_history_without_inventing_past_events(work_case):
    from test_work_migration import remove_reconciliation_schema, snapshot

    database, owner, _, _, _, _, _ = work_case
    task = create(work_case)
    with database.connect(write=True) as connection:
        remove_reconciliation_schema(connection)
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
        connection.execute("PRAGMA user_version=14")
    before = snapshot(database)
    database.initialize()
    after = snapshot(database)
    assert all(after[name] == rows for name, rows in before.items())
    assert after["work_events"] == after["work_results"] == []
    assert WorkProgress(database).events(owner, task["id"]) == {
        "events": [],
        "next_after": 0,
        "has_more": False,
    }

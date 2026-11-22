import sqlite3
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from uuid import uuid4

import pytest
from test_runs import pending
from test_schedules import configured, timestamp

from milo_home.database import Database
from milo_home.dispatch import DispatchStore
from milo_home.errors import ApiError
from milo_home.run_queue import queue_run
from milo_home.wakeup_events import EventPlanner


def make_case(client, count=2):
    thread, runs = pending(client, count)
    now = [timestamp("10:00")]
    runs.clock = lambda: now[0]
    schedule = configured(client, "10:00")
    schedule.clock = runs.clock
    planner = EventPlanner(runs.database, runs.clock)
    planner.plan()
    now[0] += 15
    return thread, runs, DispatchStore(runs.database, runs.clock), now


def test_concurrent_reservations_freeze_one_scope_and_hide_token(client):
    thread, runs, dispatch, _ = make_case(client)
    with ThreadPoolExecutor(max_workers=4) as executor:
        tickets = list(executor.map(lambda _: dispatch.reserve(60), range(4)))
    ticket = next(ticket for ticket in tickets if ticket is not None)
    assert sum(ticket is not None for ticket in tickets) == 1
    assert ticket.token not in repr(ticket)
    run = runs.get(ticket.run_id)
    assert run["thread_id"] == thread and run["status"] == "queued"
    assert ticket.lease_until <= run["deadline_at"]
    response = client.post(
        f"/api/threads/{thread}/messages",
        headers={"Idempotency-Key": str(uuid4())},
        json={"body": "预约后才送到的合成留言"},
    )
    assert response.status_code == 201
    assert len(runs.snapshot(ticket.run_id)["selected"]) == 2
    assert response.json()["id"] not in {
        message["id"] for message in runs.snapshot(ticket.run_id)["selected"]
    }
    assert ticket.token not in client.get(f"/api/threads/{thread}/runs").text
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM wake_run_events").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT count(*) FROM wake_event_delivery WHERE attempts = 1 AND run_id = ?",
                (ticket.run_id,),
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute("SELECT token_hash FROM wake_dispatches").fetchone()[0]
            != ticket.token
        )


def test_busy_and_stale_working_window_do_not_consume_attempts(client):
    thread, runs, dispatch, now = make_case(client)
    manual = runs.queue(thread, 60)
    assert dispatch.reserve() is None
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM wake_event_delivery").fetchone()[0] == 0
    runs.finish_failed(manual, "execution_failed")
    now[0] = timestamp("12:00")
    assert dispatch.reserve() is None
    now[0] = timestamp("14:00")
    assert dispatch.reserve() is not None


def test_planning_does_not_reoffer_reserved_messages(client):
    _, runs, dispatch, _ = make_case(client)
    ticket = dispatch.reserve()
    assert ticket is not None
    assert EventPlanner(runs.database, runs.clock).plan()["candidates"] == []
    assert dispatch.reserve() is None


def test_alarm_is_consumed_atomically_with_reservation(client):
    _, runs, dispatch, now = make_case(client)
    now[0] = timestamp("16:00")
    ticket = dispatch.reserve()
    assert ticket is not None
    with runs.database.connect() as connection:
        alarm = connection.execute(
            "SELECT id, state FROM wake_events WHERE kind = 'fallback'"
        ).fetchone()
        assert alarm["state"] == "done"
        assert connection.execute(
            "SELECT 1 FROM wake_run_events WHERE run_id = ? AND event_id = ?",
            (ticket.run_id, alarm["id"]),
        ).fetchone()


def test_invalid_frozen_scope_rolls_back_without_run(client):
    thread, runs, _, now = make_case(client)
    with pytest.raises(ApiError, match="scope_changed"):
        with runs.database.connect(write=True) as connection:
            queue_run(connection, thread, 60, now[0], [str(uuid4())])
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    with pytest.raises(ValueError, match="transaction"):
        with runs.database.connect() as connection:
            queue_run(connection, thread, 60, now[0])


def test_reservation_failure_rolls_back_run_events_and_alarm(client):
    _, runs, dispatch, now = make_case(client)
    now[0] = timestamp("16:00")
    with runs.database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_reservation BEFORE INSERT ON wake_dispatches "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        dispatch.reserve()
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM wake_run_events").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM wake_events WHERE kind = 'fallback'"
            ).fetchone()[0]
            == 0
        )


def test_v4_upgrade_rolls_back_and_keeps_events(client, tmp_path, monkeypatch):
    from milo_home import database as module

    _, runs, _, _ = make_case(client)
    path = tmp_path / "actual-v4.sqlite3"
    path.touch(mode=0o600)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for version, name in module.MIGRATIONS:
            if version <= 4:
                connection.executescript(
                    files("milo_home").joinpath("migrations", name).read_text()
                )
        connection.execute("PRAGMA user_version=4")
        for table in ["schedule_state", "wake_event_cursor"]:
            connection.execute(f"DELETE FROM {table}")
        with runs.database.connect() as source:
            for table in [
                "users",
                "threads",
                "messages",
                "schedule_state",
                "wake_events",
                "wake_event_cursor",
            ]:
                columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
                rows = source.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
                placeholders = ", ".join("?" for _ in columns)
                connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
    database = Database(path)
    broken = tmp_path / "broken-v5.sql"
    broken.write_text(
        "CREATE TABLE partial_dispatch (id INTEGER);\nINSERT INTO missing VALUES (1);\n"
    )
    original = module.files

    class Resources:
        def joinpath(self, *parts):
            return (
                broken
                if parts[-1] == "005_automatic_dispatch.sql"
                else original("milo_home").joinpath(*parts)
            )

    with monkeypatch.context() as context:
        context.setattr(module, "files", lambda package: Resources())
        with pytest.raises(sqlite3.OperationalError, match="no such table: missing"):
            database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT count(*) FROM wake_events").fetchone()[0] == 2
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'partial_dispatch'"
        ).fetchone()
    database.initialize()
    database.initialize()
    destination = tmp_path / "dispatch-backup.sqlite3"
    database.backup(destination)
    restored = Database(destination)
    restored.initialize()
    assert EventPlanner(restored, runs.clock).status()["counts"][0]["count"] == 2
    with restored.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

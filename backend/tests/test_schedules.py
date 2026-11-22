import sqlite3
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_runs import pending, result

from milo_home.database import CURRENT_SCHEMA_VERSION, Database
from milo_home.errors import ApiError
from milo_home.schedule_models import SchedulePolicy
from milo_home.schedule_policy import BEIJING, describe
from milo_home.schedules import ScheduleStore

EXAMPLE = Path(__file__).parents[2] / "examples/schedule.json"


def timestamp(clock, date="2026-10-14"):
    return int(datetime.fromisoformat(f"{date}T{clock}:00").replace(tzinfo=BEIJING).timestamp())


def configured(client, clock="09:00"):
    store = ScheduleStore(client.app.state.database, lambda: timestamp(clock))
    store.configure(SchedulePolicy.model_validate_json(EXAMPLE.read_bytes()))
    return store


@pytest.mark.parametrize(
    "clock,phase,next_change",
    [
        ("08:59", "resting", "09:00"),
        ("09:00", "working", "12:00"),
        ("12:00", "resting", "14:00"),
        ("14:00", "working", "21:30"),
        ("21:30", "winding_down", "22:30"),
        ("22:30", "resting", "09:00"),
    ],
)
def test_beijing_half_open_window_boundaries(client, clock, phase, next_change):
    store = configured(client, clock)
    view = store.view()
    next_date = "2026-10-15" if clock == "22:30" else "2026-10-14"
    assert view.phase == phase
    assert view.next_change_at == timestamp(next_change, next_date)
    assert view.automatic_wakeup is False


def test_weekend_explicit_day_off_and_no_private_dates_in_api(client):
    store = configured(client)
    store.clock = lambda: timestamp("09:00", "2026-10-17")
    assert store.view().source == "relaxed" and store.view().phase == "resting"
    policy, state = store.snapshot()
    data = policy.model_dump(mode="json")
    data["relaxed_dates"] = ["2026-10-14"]
    store.configure(SchedulePolicy.model_validate(data))
    view = describe(store.snapshot()[0], state, timestamp("10:00"))
    assert view.phase == "working" and view.source == "relaxed"
    response = client.get("/api/schedule")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "relaxed_dates" not in response.text and "policy" not in response.json()
    assert response.json()["automatic_wakeup"] is False
    assert client.post("/api/schedule", json={"paused": False}).status_code == 405


def test_override_expiry_pause_and_clear_do_not_replay(client):
    store = configured(client, "14:00")
    until = timestamp("17:00")
    store.override("resting", until)
    assert store.view().phase == "resting" and store.view().source == "override"
    assert store.view().next_change_at == until
    assert store.view().next_fallback_at == timestamp("21:00")
    store.pause(True)
    assert store.view().phase == "paused"
    assert store.view().next_fallback_at is None and store.view().next_change_at is None
    reopened = ScheduleStore(Database(store.database.path), lambda: until)
    assert reopened.view().paused
    reopened.pause(False)
    assert reopened.view().phase == "working" and reopened.view().source == "weekly"
    assert reopened.view().override_ends_at is None
    assert reopened.view().next_fallback_at == timestamp("21:00")
    reopened.override("winding_down", timestamp("18:00"))
    reopened.override(None, None)
    assert reopened.view().source == "weekly"


def test_pause_atomically_stops_active_run_and_blocks_new_claim(client):
    thread, runs = pending(client, 1)
    store = ScheduleStore(runs.database)
    run = runs.queue(thread, 60)
    runs.started(run)
    store.pause(True)
    assert runs.get(run)["status"] == "stopping"
    assert not runs.publish(run, result(runs, run))
    with pytest.raises(ApiError, match="schedule_paused"):
        runs.queue(thread, 60)
    runs.finish_failed(run, "execution_failed")
    assert runs.get(run)["status"] == "cancelled"
    store.pause(False)
    assert runs.queue(thread, 60) != run


@pytest.mark.parametrize("invalid", ["overlap", "night", "rest_alarm", "duplicate", "timezone"])
def test_invalid_policy_is_rejected(invalid):
    data = SchedulePolicy.model_validate_json(EXAMPLE.read_bytes()).model_dump(mode="json")
    if invalid == "overlap":
        data["weekdays"]["windows"][1]["start"] = "11:30"
    elif invalid == "night":
        data["weekdays"]["windows"][0]["start"] = "23:00"
    elif invalid == "rest_alarm":
        data["weekdays"]["fallback"] = ["12:00"]
    elif invalid == "duplicate":
        data["weekdays"]["fallback"] = ["09:00", "09:00"]
    else:
        data["timezone"] = "UTC"
    with pytest.raises(ValidationError):
        SchedulePolicy.model_validate(data)


def test_unconfigured_and_invalid_override_leave_state_unchanged(client):
    store = ScheduleStore(client.app.state.database, lambda: timestamp("14:00"))
    assert store.view().phase == "unconfigured" and store.view().today is None
    with pytest.raises(ValueError, match="Configure"):
        store.override("working", timestamp("15:00"))
    store = configured(client, "14:00")
    before = store.view().model_dump()
    for deadline in [timestamp("14:00"), timestamp("14:01", "2026-10-15")]:
        with pytest.raises(ValueError):
            store.override("working", deadline)
    assert store.view().model_dump() == before


def test_empty_days_and_end_of_day_bounds(client):
    policy = SchedulePolicy.model_validate(
        {
            "weekdays": {
                "windows": [],
                "fallback": [],
            },
            "days_off": {
                "windows": [
                    {
                        "start": "23:00",
                        "end": "24:00",
                        "phase": "working",
                    }
                ],
                "fallback": ["23:30"],
            },
        }
    )
    store = ScheduleStore(client.app.state.database, lambda: timestamp("14:00"))
    store.configure(policy)
    assert store.view().next_change_at == timestamp("00:00", "2026-10-17")
    assert store.view().next_fallback_at == timestamp("23:30", "2026-10-17")


def test_v2_migration_and_schedule_backup_preserve_records(tmp_path):
    path = tmp_path / "v2.sqlite3"
    path.touch(mode=0o600)
    database = Database(path)
    with database.connect() as connection:
        for name in ["001_initial.sql", "002_manual_runs.sql"]:
            connection.executescript(files("milo_home").joinpath("migrations", name).read_text())
        connection.execute("PRAGMA user_version=2")
        connection.execute(
            "INSERT INTO users VALUES (?, 'visitor', '访客', 'synthetic', 1)", (str(uuid4()),)
        )
    database.initialize()
    store = ScheduleStore(database)
    store.configure(SchedulePolicy.model_validate_json(EXAMPLE.read_bytes()))
    store.pause(True)
    destination = tmp_path / "backup.sqlite3"
    database.backup(destination)
    restored = Database(destination)
    restored.initialize()
    with restored.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert connection.execute("SELECT handle FROM users").fetchone()[0] == "visitor"
    assert ScheduleStore(restored).view().paused


def test_v3_migration_failure_does_not_leave_partial_state(tmp_path, monkeypatch):
    from milo_home import database as module

    database = Database(tmp_path / "rollback.sqlite3")
    database.initialize()
    with database.connect(write=True) as connection:
        connection.execute("DROP TABLE schedule_state")
        connection.execute("PRAGMA user_version=2")
    broken = tmp_path / "broken.sql"
    broken.write_text("CREATE TABLE partial_state (id INTEGER);\nINSERT INTO missing VALUES (1);\n")
    original = module.files

    class Resources:
        def joinpath(self, *parts):
            if parts[-1] == "003_schedule.sql":
                return broken
            return original("milo_home").joinpath(*parts)

    monkeypatch.setattr(module, "files", lambda package: Resources())
    with pytest.raises(sqlite3.OperationalError, match="no such table: missing"):
        database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN ('partial_state', 'schedule_state')"
        ).fetchone()

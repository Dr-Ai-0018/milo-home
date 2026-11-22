import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime

import pytest
from test_dispatch import make_case
from test_runs import pending

from milo_home.schedule_policy import BEIJING
from milo_home.usage import inspect_usage


def test_reservations_are_not_reported_as_launches_or_billing(client):
    _, runs, dispatch, now = make_case(client)
    ticket = dispatch.reserve(60)
    before = inspect_usage(runs.database.path, clock=runs.clock)
    assert before["automatic_reservations_created"] == 1
    assert before["automatic_launch_permissions_recorded"] == 0
    assert before["current_active_automatic_runs"]["count"] == 1
    assert dispatch.authorize_launch(ticket)
    after = inspect_usage(runs.database.path, clock=runs.clock)
    assert after["automatic_launch_permissions_recorded"] == 1
    assert after["tokens"] is None and after["cost"] is None
    assert after["billing_status"] == "unavailable"
    assert after["daily_budget_enforced"] is False
    assert after["automatic_wakeup"] is False
    encoded = json.dumps(after)
    assert ticket.run_id not in encoded and ticket.token not in encoded
    assert now[0] == after["observed_at"]


def test_beijing_day_boundaries_and_unfinished_carryover(client):
    _, runs, dispatch, now = make_case(client)
    ticket = dispatch.reserve(60)
    assert dispatch.authorize_launch(ticket)
    day = datetime.fromtimestamp(now[0], BEIJING).date()
    midnight = int(datetime.combine(day, datetime.min.time(), BEIJING).timestamp())
    with runs.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE runs SET created_at = ?, deadline_at = ? WHERE id = ?",
            (midnight - 1, midnight + 60, ticket.run_id),
        )
        connection.execute(
            "UPDATE wake_dispatches SET launched_at = ? WHERE run_id = ?",
            (midnight, ticket.run_id),
        )
    report = inspect_usage(runs.database.path, day, clock=runs.clock)
    assert report["automatic_reservations_created"] == 0
    assert report["automatic_launch_permissions_recorded"] == 1
    assert report["current_active_automatic_runs"] == {
        "count": 1,
        "carried_from_earlier_days": 1,
    }
    # Past the deadline is still an active occupancy, not a free budget slot.
    assert runs.active()["id"] == ticket.run_id
    with runs.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE wake_dispatches SET launched_at = ? WHERE run_id = ?",
            (midnight + 86400, ticket.run_id),
        )
    report = inspect_usage(runs.database.path, day, clock=runs.clock)
    assert report["automatic_launch_permissions_recorded"] == 0
    assert report["clock_ahead"] is True


def test_manual_runs_do_not_count_as_automatic(client):
    thread, runs = pending(client, 1)
    runs.queue(thread, 60)
    report = inspect_usage(runs.database.path)
    assert report["automatic_reservations_created"] == 0
    assert report["automatic_launch_permissions_recorded"] == 0
    assert report["current_active_automatic_runs"]["count"] == 0
    assert runs.active() is not None


def test_missing_database_is_not_created(tmp_path):
    missing = tmp_path / "absent.sqlite3"
    with pytest.raises(FileNotFoundError):
        inspect_usage(missing)
    assert not missing.exists()


def test_old_schema_is_not_migrated(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=4")
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="current schema"):
        inspect_usage(path)
    assert path.read_bytes() == before


def test_usage_cli_is_readonly_and_accepts_a_day(client):
    _, runs, _, _ = make_case(client)
    before = inspect_usage(runs.database.path, date(2026, 10, 16), clock=lambda: 1)
    environment = dict(os.environ, MILO_DATABASE=str(runs.database.path))
    process = subprocess.run(
        [sys.executable, "-m", "milo_home.cli", "usage", "--day", "2026-10-16"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["day"] == "2026-10-16"
    assert report["automatic_reservations_created"] == 0
    after = inspect_usage(runs.database.path, date(2026, 10, 16), clock=lambda: 1)
    assert before == after

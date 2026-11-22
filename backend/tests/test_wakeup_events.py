import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest

from milo_home.database import Database
from milo_home.messages import MessageService
from milo_home.reply_result import ReplyResult
from milo_home.runs import RunStore
from milo_home.schedule_models import SchedulePolicy
from milo_home.schedule_policy import BEIJING
from milo_home.schedules import ScheduleStore
from milo_home.wakeup_events import EventPlanner

EXAMPLE = Path(__file__).parents[2] / "examples/schedule.json"


def timestamp(clock, date="2026-10-16"):
    return int(datetime.fromisoformat(f"{date}T{clock}").replace(tzinfo=BEIJING).timestamp())


@pytest.fixture
def setup(tmp_path):
    database = Database(tmp_path / "events.sqlite3")
    database.initialize()
    now = [timestamp("10:00:00")]
    planner = EventPlanner(database, lambda: now[0])
    schedule = ScheduleStore(database, lambda: now[0])
    schedule.configure(SchedulePolicy.model_validate_json(EXAMPLE.read_bytes()))
    owner, thread = str(uuid4()), str(uuid4())
    with database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO users VALUES (?, 'visitor', '合成访客', 'unused', ?)", (owner, now[0])
        )
        connection.execute("INSERT INTO threads VALUES (?, ?, ?)", (thread, owner, now[0]))
    return database, now, planner, schedule, owner, thread


def post(setup, count=1):
    database, _, _, _, owner, thread = setup
    return [
        MessageService(database).post(owner, thread, "合成私信，不可打印", str(uuid4()))[0]["id"]
        for _ in range(count)
    ]


def test_debounce_is_persistent_and_scans_are_idempotent(setup):
    database, now, planner, _, _, _ = setup
    ids = post(setup)
    assert planner.plan()["waiting"] == 1
    now[0] += 14
    assert planner.plan()["candidates"] == []
    ids += post(setup)
    assert planner.plan()["discovered_messages"] == 1
    now[0] += 14
    assert planner.plan()["candidates"] == []
    now[0] += 1
    first = planner.plan()["candidates"]
    assert first[0]["message_ids"] == ids
    reopened = EventPlanner(Database(database.path), lambda: now[0])
    assert reopened.plan()["candidates"] == first
    assert reopened.status()["counts"] == [
        {
            "kind": "message",
            "state": "pending",
            "count": 2,
        }
    ]
    assert "合成私信" not in json.dumps(reopened.plan(), ensure_ascii=False)


def test_continuous_messages_cannot_postpone_forever(setup):
    _, now, planner, _, _, _ = setup
    post(setup)
    planner.plan()
    for _ in range(5):
        now[0] += 10
        post(setup)
        assert planner.plan()["candidates"] == []
    now[0] += 10
    post(setup)
    assert len(planner.plan()["candidates"][0]["message_ids"]) == 7


def test_first_ten_messages_are_not_delayed_by_eleventh(setup):
    _, now, planner, _, _, _ = setup
    ids = post(setup, 10)
    planner.plan()
    now[0] += 15
    post(setup)
    candidate = planner.plan()["candidates"][0]
    assert candidate["message_ids"] == ids
    assert candidate["cause"] == "message"


def test_retry_candidate_uses_actual_retry_ready_time(setup):
    database, now, planner, *_ = setup
    post(setup)
    planner.plan()
    with database.connect(write=True) as connection:
        event = connection.execute("SELECT id FROM wake_events WHERE kind='message'").fetchone()[0]
        connection.execute(
            "INSERT INTO wake_event_delivery (event_id,attempts,retry_after) VALUES (?,1,?)",
            (event, now[0] + 40),
        )
    now[0] += 39
    assert planner.plan()["candidates"] == []
    now[0] += 1
    assert planner.plan()["candidates"][0]["ready_at"] == now[0]


@pytest.mark.parametrize("phase", ["resting", "winding_down", "paused", "unconfigured"])
def test_nonworking_states_keep_messages_but_offer_no_execution(setup, phase):
    database, now, planner, schedule, _, _ = setup
    if phase == "paused":
        schedule.pause(True)
    elif phase == "unconfigured":
        with database.connect(write=True) as connection:
            connection.execute("UPDATE schedule_state SET policy = NULL WHERE id = 1")
    else:
        schedule.override(phase, now[0] + 60)
    post(setup)
    result = planner.plan()
    assert result["phase"] == phase
    assert result["candidates"] == []
    now[0] += 30
    assert planner.plan()["candidates"] == []
    assert planner.status()["counts"] == [
        {
            "kind": "message",
            "state": "pending",
            "count": 1,
        }
    ]


def test_rest_messages_become_ready_on_work_without_restarting_debounce(setup):
    _, now, planner, schedule, _, _ = setup
    now[0] = timestamp("08:00:00")
    ids = post(setup)
    planner.plan()
    now[0] = timestamp("09:00:01")
    candidate = planner.plan()["candidates"][0]
    assert candidate["message_ids"] == ids
    assert candidate["cause"] == "fallback"
    schedule.pause(True)
    assert planner.plan()["candidates"] == []
    schedule.pause(False)
    assert planner.plan()["candidates"][0]["cause"] == "message"


def test_alarm_grace_and_global_thread_isolation(setup):
    database, now, planner, _, _, thread = setup
    post(setup)
    other_owner, other_thread = str(uuid4()), str(uuid4())
    with database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO users VALUES (?, 'other', '合成访客二', 'unused', ?)",
            (other_owner, now[0]),
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?)", (other_thread, other_owner, now[0])
        )
    other_message = MessageService(database).post(other_owner, other_thread, "样例", str(uuid4()))[
        0
    ]
    now[0] = timestamp("15:59:59")
    planner.plan()
    now[0] = timestamp("16:00:01")
    candidates = planner.plan()["candidates"]
    assert len(candidates) == 2
    by_thread = {candidate["thread_id"]: candidate for candidate in candidates}
    assert by_thread[other_thread]["message_ids"] == [other_message["id"]]
    assert other_message["id"] not in by_thread[thread]["message_ids"]
    assert all(candidate["cause"] == "fallback" for candidate in candidates)
    again = planner.plan()["candidates"]
    assert again == candidates
    now[0] = timestamp("16:01:01")
    assert all(candidate["cause"] == "message" for candidate in planner.plan()["candidates"])


@pytest.mark.parametrize("start", ["16:00:01", "16:01:01", "23:59:59"])
def test_first_scan_never_replays_missed_alarms(setup, start):
    _, now, planner, _, _, _ = setup
    post(setup)
    now[0] = timestamp(start)
    planner.plan()
    assert not any(row["kind"] == "fallback" for row in planner.status()["counts"])


def test_exact_alarm_is_recorded_once_and_no_empty_alarm_is_created(setup):
    _, now, planner, _, _, _ = setup
    now[0] = timestamp("16:00:00")
    assert planner.plan()["candidates"] == []
    assert planner.status()["counts"] == []
    ids = post(setup)
    # This second scan is the same instant, so the already-observed alarm is not replayed.
    assert planner.plan()["waiting"] == 1
    now[0] = timestamp("21:00:00")
    candidate = planner.plan()["candidates"][0]
    assert candidate["cause"] == "fallback"
    assert candidate["message_ids"] == ids
    planner.plan()
    assert sum(row["count"] for row in planner.status()["counts"] if row["kind"] == "fallback") == 1


def test_schedule_revision_does_not_retroactively_add_alarm(setup):
    _, now, planner, schedule, _, _ = setup
    post(setup)
    now[0] = timestamp("15:59:59")
    planner.plan()
    now[0] = timestamp("16:00:01")
    schedule.configure(schedule.snapshot()[0])
    assert planner.plan()["candidates"] == []
    assert not any(row["kind"] == "fallback" for row in planner.status()["counts"])


def test_clock_rollback_does_not_move_cursor_or_make_candidates(setup):
    _, now, planner, _, _, _ = setup
    post(setup)
    planner.plan()
    original = planner.status()["cursor"]
    now[0] -= 1
    assert planner.plan()["phase"] == "clock_rewind"
    assert planner.status()["cursor"] == original
    now[0] += 16
    assert len(planner.plan()["candidates"]) == 1


def test_concurrent_scans_do_not_duplicate_events(setup):
    database, now, _, _, _, _ = setup
    ids = post(setup, 10)
    now[0] = timestamp("16:00:00")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: EventPlanner(database, lambda: now[0]).plan(), range(2)))
    assert results[0]["candidates"] == results[1]["candidates"]
    assert sum(result["discovered_messages"] for result in results) == 10
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM wake_events").fetchone()[0] == len(ids) + 1


def test_manual_reply_settles_events_and_backup_preserves_cursor(setup, tmp_path):
    database, now, planner, _, _, thread = setup
    ids = post(setup, 2)
    now[0] = timestamp("16:00:00")
    planner.plan()
    runs = RunStore(database, lambda: now[0])
    run_id = runs.queue(thread, 180)
    runs.started(run_id)
    result = ReplyResult.model_validate(
        {
            "replies": [
                {
                    "message_id": id_,
                    "body": "合成回信",
                }
                for id_ in ids
            ]
        }
    )
    assert runs.publish(run_id, result)
    assert planner.plan()["candidates"] == []
    counts = planner.status()["counts"]
    assert all(row["state"] == "done" for row in counts)
    database.backup(tmp_path / "restored.sqlite3")
    restored = Database(tmp_path / "restored.sqlite3")
    restored.initialize()
    assert EventPlanner(restored, lambda: now[0]).status() == planner.status()
    with restored.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v3_upgrade_and_failure_rollback(tmp_path, monkeypatch):
    from milo_home import database as module

    database = Database(tmp_path / "v3.sqlite3")
    database.path.touch(mode=0o600)
    with database.connect() as connection:
        for name in ["001_initial.sql", "002_manual_runs.sql", "003_schedule.sql"]:
            connection.executescript(files("milo_home").joinpath("migrations", name).read_text())
        connection.execute("PRAGMA user_version=3")
        connection.execute("UPDATE schedule_state SET paused = 1 WHERE id = 1")
    broken = tmp_path / "broken.sql"
    broken.write_text("CREATE TABLE partial_event (id INTEGER);\nINSERT INTO missing VALUES (1);\n")
    original = module.files

    class Resources:
        def joinpath(self, *parts):
            if parts[-1] == "004_wakeup_events.sql":
                return broken
            return original("milo_home").joinpath(*parts)

    with monkeypatch.context() as context:
        context.setattr(module, "files", lambda package: Resources())
        with pytest.raises(sqlite3.OperationalError, match="no such table: missing"):
            database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'partial_event'"
        ).fetchone()
    database.initialize()
    database.initialize()
    assert ScheduleStore(database).view().paused
    assert EventPlanner(database).status()["counts"] == []


def test_real_cli_plans_without_printing_messages_or_creating_runs(setup):
    database, _, _, _, _, _ = setup
    post(setup)
    for action in ["plan", "status"]:
        process = subprocess.run(
            [sys.executable, "-m", "milo_home.cli", "events", action],
            env={**os.environ, "MILO_DATABASE": str(database.path)},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert process.returncode == 0, process.stderr
        assert json.loads(process.stdout)["automatic_wakeup"] is False
        assert "合成私信" not in process.stdout + process.stderr
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("delay,has_alarm", [(60, True), (61, False)])
def test_alarm_grace_has_exact_sixty_second_boundary(setup, delay, has_alarm):
    _, now, planner, _, _, _ = setup
    post(setup)
    now[0] = timestamp("15:59:59")
    planner.plan()
    now[0] = timestamp("16:00:00") + delay
    candidate = planner.plan()["candidates"][0]
    assert (candidate["cause"] == "fallback") == has_alarm


def test_alarm_uses_new_beijing_date_at_midnight(setup):
    _, now, planner, schedule, _, _ = setup
    all_day = [
        {
            "start": "00:00",
            "end": "24:00",
            "phase": "working",
        }
    ]
    schedule.configure(
        SchedulePolicy.model_validate(
            {
                "weekdays": {
                    "windows": all_day,
                    "fallback": ["23:59"],
                },
                "days_off": {
                    "windows": all_day,
                    "fallback": ["00:00"],
                },
            }
        )
    )
    post(setup)
    now[0] = timestamp("23:59:59")
    planner.plan()
    now[0] = timestamp("00:00:00", "2026-10-17")
    candidate = planner.plan()["candidates"][0]
    assert candidate["cause"] == "fallback"
    assert candidate["ready_at"] == now[0]


def test_scan_failure_rolls_back_events_and_cursor(setup, monkeypatch):
    database, _, planner, _, _, _ = setup
    post(setup)
    before = planner.status()
    original = EventPlanner._discover

    def fail_after_discovery(connection, now):
        original(connection, now)
        raise ValueError("synthetic failure")

    monkeypatch.setattr(EventPlanner, "_discover", staticmethod(fail_after_discovery))
    with pytest.raises(ValueError, match="synthetic failure"):
        planner.plan()
    assert EventPlanner(database).status() == before


def test_bounded_discovery_continues_without_dropping_old_messages(setup):
    database, now, planner, _, owner, thread = setup
    ids = [str(uuid4()) for _ in range(1002)]
    with database.connect(write=True) as connection:
        connection.executemany(
            "INSERT INTO messages "
            "(id, thread_id, sequence, author_id, role, body, client_key, created_at) "
            "VALUES (?, ?, ?, ?, 'visitor', '合成留言', ?, ?)",
            [
                (id_, thread, index, owner, str(uuid4()), now[0] - index)
                for index, id_ in enumerate(ids, 1)
            ],
        )
    assert planner.plan()["discovered_messages"] == 1000
    now[0] += 15
    result = planner.plan()
    assert result["discovered_messages"] == 2
    assert result["candidates"][0]["message_ids"] == ids[:10]
    assert planner.status()["counts"][0]["count"] == 1002

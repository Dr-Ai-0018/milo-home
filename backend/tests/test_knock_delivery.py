import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from conftest import activate, own_thread
from test_automatic_worker import successful_execute
from test_dispatch import make_case
from test_knocks import knock
from test_runs import result
from test_schedules import timestamp
from test_worker import fake_runtime

from milo_home import worker as worker_module
from milo_home.automatic_budget import AutomaticPolicy
from milo_home.database import Database
from milo_home.dispatch import DispatchStore
from milo_home.errors import ApiError
from milo_home.knock_delivery import pending_knock
from milo_home.reply_result import make_prompt
from milo_home.run_queue import queue_run
from milo_home.runs import RunStore
from milo_home.schedules import ScheduleStore
from milo_home.wakeup_events import EventPlanner
from milo_home.worker import Worker


def case(client, count=1):
    thread, runs, dispatch, now = make_case(client, count)
    client.app.state.knocks.clock = lambda: now[0]
    return thread, runs, dispatch, now


def greeting(runs, conversation, now):
    with runs.database.connect() as connection:
        signal = pending_knock(connection, conversation, now[0])
        return dict(signal) if signal else None


def send(client, conversation):
    response = client.post(
        f"/api/conversations/{conversation}/messages",
        headers={"Idempotency-Key": str(uuid4())},
        json={"body": "下一轮合成消息"},
    )
    assert response.status_code == 201
    return response.json()


def test_knock_marks_existing_candidate_without_priority_or_debounce_changes(client):
    thread, runs, dispatch, now = case(client)
    now[0] -= 15
    planner = EventPlanner(runs.database, runs.clock)
    before = planner.plan()
    receipt = knock(client, thread).json()
    assert planner.plan() == before
    assert before["waiting"] == 1 and dispatch.reserve(60) is None
    now[0] += 15
    candidate = planner.plan()["candidates"][0]
    assert candidate["knock_received_at"] == receipt["received_at"]
    assert candidate["cause"] == "message" and candidate["ready_at"] == now[0]
    ticket = dispatch.reserve(60)
    assert runs.snapshot(ticket.run_id)["knock"]["received_at"] == receipt["received_at"]
    assert len(candidate["event_ids"]) == len(candidate["message_ids"]) == 1


def test_knock_without_messages_does_not_create_candidate_or_run(client):
    thread, runs, dispatch, now = case(client, count=0)
    knock(client, thread)
    assert EventPlanner(runs.database, runs.clock).plan()["candidates"] == []
    assert dispatch.reserve(60) is None
    assert greeting(runs, thread, now) is not None
    with runs.database.connect() as connection:
        for table in ["runs", "run_knocks", "wake_events", "wake_event_delivery"]:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("phase", ["resting", "winding_down", "paused", "unconfigured"])
def test_nonworking_knock_only_waits_for_normal_work(client, phase):
    thread, runs, dispatch, now = case(client)
    schedule = ScheduleStore(runs.database, runs.clock)
    if phase == "paused":
        schedule.pause(True)
    elif phase == "unconfigured":
        with runs.database.connect(write=True) as connection:
            connection.execute("UPDATE schedule_state SET policy = NULL")
    else:
        now[0] = timestamp("23:00" if phase == "resting" else "21:30")
    receipt = knock(client, thread).json()
    assert receipt["response_phase"] == ("unknown" if phase == "unconfigured" else phase)
    assert dispatch.reserve(60) is None
    assert greeting(runs, thread, now) is not None
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM run_knocks").fetchone()[0] == 0


def test_latest_signal_is_frozen_and_success_covers_older_greetings_only(client):
    thread, runs, _, now = case(client)
    for _ in range(3):
        receipt = knock(client, thread).json()
        now[0] += 30
    run = runs.queue(thread, 180)
    snapshot = runs.snapshot(run)
    assert snapshot["knock"] == {
        "source": "saved_greeting_for_this_conversation",
        "received_at": receipt["received_at"],
    }
    later = knock(client, thread).json()
    assert later["disposition"] == "accepted"
    assert runs.snapshot(run) == snapshot
    runs.started(run)
    assert runs.publish(run, result(runs, run))
    assert greeting(runs, thread, now)["id"] == later["id"]
    send(client, thread)
    second = runs.queue(thread, 60)
    assert runs.snapshot(second)["knock"]["received_at"] == later["received_at"]
    runs.started(second)
    assert runs.publish(second, result(runs, second))
    reopened = RunStore(Database(runs.database.path), runs.clock)
    assert greeting(reopened, thread, now) is None
    send(client, thread)
    assert reopened.snapshot(reopened.queue(thread, 60))["knock"] is None


def test_knock_after_queue_in_same_second_is_not_added_to_frozen_input(client):
    thread, runs, _, now = case(client)
    run = runs.queue(thread, 60)
    before = runs.snapshot(run)
    receipt = knock(client, thread).json()
    assert receipt["received_at"] == before["created_at"]
    assert runs.snapshot(run) == before and before["knock"] is None
    runs.started(run)
    assert runs.publish(run, result(runs, run))
    assert greeting(runs, thread, now)["id"] == receipt["id"]


def test_cross_topic_coalescing_does_not_import_the_first_topic_greeting(client):
    thread, runs, _, now = case(client)
    chat = client.put(f"/api/threads/{thread}/chat").json()["id"]
    chat_message = send(client, chat)
    first = knock(client, thread).json()
    assert knock(client, chat).json()["disposition"] == "coalesced"
    assert greeting(runs, chat, now) is None
    run = runs.queue(thread, 60)
    assert runs.snapshot(run)["knock"]["received_at"] == first["received_at"]
    runs.started(run)
    assert runs.publish(run, result(runs, run))
    chat_run = runs.queue(thread, 60)
    assert runs.snapshot(chat_run)["knock"] is None
    assert runs.snapshot(chat_run)["selected"][0]["id"] == chat_message["id"]
    runs.finish_failed(chat_run, "execution_failed")
    now[0] += 30
    independent = knock(client, chat).json()
    chat_run = runs.queue(thread, 60)
    assert runs.snapshot(chat_run)["knock"]["received_at"] == independent["received_at"]
    assert greeting(runs, thread, now) is None


def test_other_owner_greeting_never_enters_run_or_prompt(client):
    thread, runs, _, now = case(client)
    activate(client, "other-owner")
    other_thread = own_thread(client)
    receipt = knock(client, other_thread).json()
    run = runs.queue(thread, 60)
    assert runs.snapshot(run)["knock"] is None
    prompt = make_prompt(runs.snapshot(run))
    assert receipt["id"] not in prompt and other_thread not in prompt
    assert greeting(runs, other_thread, now)["id"] == receipt["id"]


def test_safe_prelaunch_retry_keeps_greeting_and_original_backoff(client):
    thread, runs, dispatch, now = case(client)
    receipt = knock(client, thread).json()
    ticket = dispatch.reserve(60)
    runs.finish_failed(ticket.run_id, "interrupted")
    assert dispatch.reconcile()["retrying_messages"] == 1
    now[0] += 1
    assert knock(client, thread).json()["disposition"] == "coalesced"
    assert dispatch.reserve(60) is None
    now[0] += 29
    retry = dispatch.reserve(60)
    assert runs.snapshot(retry.run_id)["knock"]["received_at"] == receipt["received_at"]
    assert dispatch.authorize_launch(retry)
    runs.started(retry.run_id)
    assert runs.publish(retry.run_id, result(runs, retry.run_id))
    assert greeting(runs, thread, now) is None


@pytest.mark.parametrize("failure", ["cancelled", "timed_out", "invalid_result", "unknown"])
def test_greeting_does_not_unlock_failed_or_unknown_execution(client, failure):
    thread, runs, dispatch, now = case(client)
    knock(client, thread)
    ticket = dispatch.reserve(60)
    assert dispatch.authorize_launch(ticket)
    runs.started(ticket.run_id)
    if failure in {"cancelled", "timed_out", "unknown"}:
        runs.stop(ticket.run_id, "timed_out" if failure == "timed_out" else "cancelled")
    if failure != "unknown":
        runs.finish_failed(ticket.run_id, failure)
        dispatch.reconcile()
    with runs.database.connect() as connection:
        before = [dict(row) for row in connection.execute("SELECT * FROM wake_event_delivery")]
    now[0] += 30
    knock(client, thread)
    assert dispatch.reserve(60) is None
    assert greeting(runs, thread, now) is not None
    with runs.database.connect() as connection:
        assert [
            dict(row) for row in connection.execute("SELECT * FROM wake_event_delivery")
        ] == before


def test_exhausted_budget_does_not_attach_or_consume_new_greeting(client):
    thread, runs, _, now = case(client)
    dispatch = DispatchStore(
        runs.database, runs.clock, policy=AutomaticPolicy(daily_launches=1, daily_seconds=None)
    )
    knock(client, thread)
    first = dispatch.reserve(60)
    assert dispatch.authorize_launch(first)
    runs.started(first.run_id)
    assert runs.publish(first.run_id, result(runs, first.run_id))
    dispatch.reconcile()
    now[0] += 30
    receipt = knock(client, thread).json()
    send(client, thread)
    EventPlanner(runs.database, runs.clock).plan()
    now[0] += 15
    with pytest.raises(ApiError, match="daily_budget_exhausted"):
        dispatch.reserve(60)
    assert greeting(runs, thread, now)["id"] == receipt["id"]
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM run_knocks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM wake_event_delivery").fetchone()[0] == 1


def test_publish_rollback_does_not_consume_greeting_or_publish_partial_reply(client):
    thread, runs, _, now = case(client)
    receipt = knock(client, thread).json()
    run = runs.queue(thread, 60)
    runs.started(run)
    with runs.database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_success BEFORE UPDATE ON runs WHEN NEW.status = 'succeeded' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic publish failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic publish failure"):
        runs.publish(run, result(runs, run))
    assert runs.get(run)["status"] == "running"
    assert greeting(runs, thread, now)["id"] == receipt["id"]
    with runs.database.connect(write=True) as connection:
        assert (
            connection.execute("SELECT count(*) FROM messages WHERE role = 'milo'").fetchone()[0]
            == 0
        )
        connection.execute("DROP TRIGGER fail_success")
    assert runs.publish(run, result(runs, run))
    assert greeting(runs, thread, now) is None


def test_concurrent_reservation_and_rollback_keep_one_atomic_greeting(client):
    thread, runs, dispatch, now = case(client)
    knock(client, thread)
    with runs.database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_dispatch BEFORE INSERT ON wake_dispatches "
            "BEGIN SELECT RAISE(ABORT, 'synthetic dispatch failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic dispatch failure"):
        dispatch.reserve(60)
    assert greeting(runs, thread, now) is not None
    with runs.database.connect(write=True) as connection:
        assert connection.execute("SELECT count(*) FROM run_knocks").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_dispatch")
    with ThreadPoolExecutor(max_workers=3) as pool:
        tickets = list(pool.map(lambda _: dispatch.reserve(60), range(3)))
    assert sum(ticket is not None for ticket in tickets) == 1
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM run_knocks").fetchone()[0] == 1


def test_future_signal_and_snapshot_reads_do_not_consume_or_change_input(client):
    thread, runs, _, now = case(client)
    receipt = knock(client, thread).json()
    now[0] -= 1
    assert greeting(runs, thread, now) is None
    now[0] += 1
    run = runs.queue(thread, 60)
    snapshot = runs.snapshot(run)
    for _ in range(3):
        assert runs.snapshot(run) == snapshot
    assert greeting(runs, thread, now)["id"] == receipt["id"]
    serialized = make_prompt(snapshot)
    assert receipt["id"] not in serialized
    assert "不证明此前已听见" in serialized and "提示不授权执行" in serialized
    assert "本输入没有提示" not in serialized


def test_database_rejects_cross_conversation_coalesced_and_future_attachment(client):
    thread, runs, _, now = case(client)
    chat = client.put(f"/api/threads/{thread}/chat").json()["id"]
    send(client, chat)
    anchor = knock(client, chat).json()
    merged = knock(client, thread).json()
    run = runs.queue(thread, 180)
    now[0] += 30
    future = knock(client, thread).json()
    with runs.database.connect(write=True) as connection:
        for receipt in [anchor, merged, future]:
            with pytest.raises(sqlite3.IntegrityError, match="scope"):
                connection.execute("INSERT INTO run_knocks VALUES (?, ?)", (run, receipt["id"]))
    runs.finish_failed(run, "execution_failed")
    run = runs.queue(thread, 60)
    with runs.database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE run_knocks SET knock_id = ?", (anchor["id"],))
    runs.started(run)
    with runs.database.connect(write=True) as connection:
        connection.execute("DELETE FROM run_knocks")
        with pytest.raises(sqlite3.IntegrityError, match="scope"):
            connection.execute("INSERT INTO run_knocks VALUES (?, ?)", (run, future["id"]))


def test_explicit_scope_keeps_candidate_knock_on_the_selected_conversation(client):
    thread, runs, _, now = case(client)
    chat = client.put(f"/api/threads/{thread}/chat").json()["id"]
    selected = send(client, chat)
    receipt = knock(client, chat).json()
    with runs.database.connect(write=True) as connection:
        run = queue_run(connection, thread, 60, now[0], [selected["id"]])
    assert runs.snapshot(run)["conversation_id"] == chat
    assert runs.snapshot(run)["knock"]["received_at"] == receipt["received_at"]


def test_consumption_keeps_original_http_receipt_retryable_without_new_signal(client):
    thread, runs, _, now = case(client)
    key = str(uuid4())
    original = knock(client, thread, key).json()
    run = runs.queue(thread, 60)
    runs.started(run)
    assert runs.publish(run, result(runs, run))
    now[0] += 300
    replay = knock(client, thread, key)
    assert replay.status_code == 200 and replay.json() == original
    assert greeting(runs, thread, now) is None
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM knocks").fetchone()[0] == 1
    assert knock(client, thread).json()["disposition"] == "accepted"
    assert greeting(runs, thread, now) is not None


@pytest.mark.parametrize("entry", ["manual", "automatic"])
def test_http_greeting_reaches_worker_prompt_and_success_without_model_calls(
    client, monkeypatch, entry
):
    thread, runs, _, now = case(client)
    receipt = knock(client, thread).json()
    inputs = []

    def prepare(base, snapshot, profile):
        root = fake_runtime(base, snapshot, profile)
        (base / "prompt.txt").write_text(make_prompt(snapshot))
        return root

    def execute(command, prompt, store, run_id):
        snapshot = json.loads(prompt.read_text().split("以下为本次唯一允许的业务输入：\n", 1)[1])
        inputs.append(snapshot)
        assert snapshot["knock"]["received_at"] == receipt["received_at"]
        assert snapshot["conversation_id"] == thread
        return successful_execute(command, prompt, store, run_id)

    monkeypatch.setattr(worker_module, "prepare", prepare)
    monkeypatch.setattr(worker_module, "execute", execute)
    monkeypatch.setattr(worker_module, "stop_unit", lambda unit: True)
    worker = Worker(runs.database, clock=runs.clock)
    run = worker.wake(thread, 60) if entry == "manual" else worker.wake_automatic(60)
    assert run["status"] == "succeeded" and len(inputs) == 1
    assert greeting(runs, thread, now) is None
    assert client.get("/api/threads").json()[0]["pending_count"] == 0
    assert not (runs.database.path.parent / "runs" / run["id"] / "credential.env").exists()

from uuid import uuid4

import pytest
from test_reply_readiness import preparation as preparation
from test_worker import fake_runtime

from milo_home.automatic_services import AutomaticServices
from milo_home.conversations import ConversationService
from milo_home.errors import ApiError
from milo_home.reply_execution import run_once
from milo_home.reply_receipts import publish_receipt
from milo_home.reply_recovery import replay
from milo_home.reply_scope import ReplyScope
from milo_home.run_transport import EventStream
from milo_home.scheduler_cli import StopRequest
from milo_home.wakeup_events import EventPlanner
from milo_home.work_access import WorkAccess
from milo_home.work_tasks import WorkTasks


@pytest.fixture
def reply_case(preparation, monkeypatch):
    database, owner, thread, schedule, directory, profile, key = preparation
    now = [int(schedule.clock())]

    def clock():
        return now[0]

    access = WorkAccess(database, clock)
    workspace = str(uuid4())
    access.register_workspace(workspace, "合成工作桌", directory)
    access.grant(owner)
    session, _ = WorkTasks(database, clock).main_session(owner, workspace)
    services = AutomaticServices(database, clock)
    services.configure("work", owner, "milo-home-work.timer", session=session["id"])
    services.configure("reply", owner, "milo-home-reply.timer", enabled=True)
    monkeypatch.setattr("milo_home.reply_execution.timer_active", lambda unit: True)
    calls = []

    def prepare(base, snapshot, profile, **kwargs):
        assert kwargs["credential"] == "synthetic-private-file-key"
        assert kwargs["configuration"]
        calls.append(snapshot)
        return fake_runtime(base, snapshot, profile)

    def execute(command, prompt, store, run_id):
        store.started(run_id)
        events = EventStream()
        events.completed = 1
        return 0, events, True

    monkeypatch.setattr("milo_home.reply_worker.prepare", prepare)
    monkeypatch.setattr("milo_home.worker.execute", execute)
    monkeypatch.setattr("milo_home.worker.stop_unit", lambda unit: True)
    return preparation, now, services, calls


def tick(case, *, stop=None):
    preparation, now, *_ = case
    database, owner, _, _, directory, profile, key = preparation
    return run_once(
        database, directory, owner, profile, key, 3600, stop or StopRequest(), clock=lambda: now[0]
    )


def send(client, case, count=1, *, conversation=None):
    preparation, now, *_ = case
    database, _, thread, *_ = preparation
    for _ in range(count):
        endpoint = (
            f"/api/conversations/{conversation}/messages"
            if conversation
            else f"/api/threads/{thread}/messages"
        )
        response = client.post(
            endpoint, headers={"Idempotency-Key": str(uuid4())}, json={"body": "合成来信"}
        )
        assert response.status_code == 201, response.text
        with database.connect(write=True) as connection:
            connection.execute(
                "UPDATE messages SET created_at=? WHERE id=?", (now[0], response.json()["id"])
            )


def ready(case):
    preparation, now, *_ = case
    EventPlanner(preparation[0], lambda: now[0], scope=ReplyScope(preparation[1])).plan()
    now[0] += 15


def latest(database):
    with database.snapshot() as connection:
        return dict(connection.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone())


def test_empty_checks_never_launch_or_reserve(reply_case):
    assert tick(reply_case)["outcome"] == "waiting_or_other_entry_first"
    assert reply_case[-1] == []
    with reply_case[0][0].snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM legacy_launches").fetchone()[0] == 0


def test_multiple_batches_return_to_original_topics_without_duplicates(client, reply_case):
    database, owner, thread, *_ = reply_case[0]
    send(client, reply_case, 12)
    chat = ConversationService(database).chat(owner, thread)
    send(client, reply_case, 2, conversation=chat["id"])
    ready(reply_case)
    for _ in range(3):
        assert tick(reply_case)["outcome"] == "succeeded"
    assert [len(snapshot["selected"]) for snapshot in reply_case[-1]] == [10, 2, 2]
    assert reply_case[-1][-1]["conversation_id"] == chat["id"]
    assert tick(reply_case)["outcome"] == "waiting_or_other_entry_first"
    with database.snapshot() as connection:
        assert (
            connection.execute("SELECT count(*) FROM messages WHERE role='milo'").fetchone()[0]
            == 14
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM legacy_launches WHERE authorized_at IS NOT NULL"
            ).fetchone()[0]
            == 3
        )
        assert connection.execute("SELECT count(*) FROM reply_execution_results").fetchone()[0] == 3
    directory = reply_case[0][4]
    assert not list(directory.glob("*/credential.env"))


def test_late_arrival_is_left_for_another_frozen_batch(client, reply_case, monkeypatch):
    send(client, reply_case)
    ready(reply_case)

    def execute(command, prompt, store, run_id):
        store.started(run_id)
        send(client, reply_case)
        events = EventStream()
        events.completed = 1
        return 0, events, True

    monkeypatch.setattr("milo_home.worker.execute", execute)
    assert tick(reply_case)["outcome"] == "succeeded"
    assert len(reply_case[-1][0]["selected"]) == 1
    assert client.get("/api/threads").json()[0]["pending_count"] == 1


def test_unknown_is_latched_and_next_tick_never_recovers_or_relaunches(
    client, reply_case, monkeypatch
):
    send(client, reply_case)
    ready(reply_case)
    monkeypatch.setattr("milo_home.worker.execute", lambda *args: (0, EventStream(), False))
    with pytest.raises(ApiError, match="termination_unknown"):
        tick(reply_case)
    database = reply_case[0][0]
    assert latest(database)["status"] == "stopping"
    reply_case[1][0] += 3601
    monkeypatch.setattr(
        "milo_home.worker.stop_unit", lambda unit: pytest.fail("No automatic recovery")
    )
    assert tick(reply_case)["outcome"] == "service_disabled_or_faulted"
    assert len(reply_case[-1]) == 1
    assert latest(database)["status"] == "stopping"


def test_upstream_failure_latches_once_without_sealing_or_automatic_retry(
    client, reply_case, monkeypatch
):
    send(client, reply_case)
    ready(reply_case)

    def unavailable(command, prompt, store, run_id):
        store.started(run_id)
        events = EventStream()
        events.failed = True
        return 1, events, True

    monkeypatch.setattr("milo_home.worker.execute", unavailable)
    assert tick(reply_case)["outcome"] == "failed"
    assert tick(reply_case)["outcome"] == "service_disabled_or_faulted"
    with reply_case[0][0].snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM reply_execution_results").fetchone()[0] == 0
        assert connection.execute("SELECT attempts FROM wake_event_delivery").fetchone()[0] == 1
        assert connection.execute("SELECT blocked_reason FROM wake_event_delivery").fetchone()[0]


@pytest.mark.parametrize("storage_failure", ["sqlite", "os", "clock"])
def test_sealed_result_recovers_publication_without_another_model_call(
    client, reply_case, monkeypatch, storage_failure
):
    send(client, reply_case)
    ready(reply_case)
    database, owner, _, _, directory, *_ = reply_case[0]
    with database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_publish BEFORE INSERT ON messages WHEN NEW.role='milo' "
            "BEGIN SELECT RAISE(ABORT,'synthetic publish outage'); END"
        )
    if storage_failure in {"os", "clock"}:

        def unavailable(*args, **kwargs):
            if storage_failure == "clock":
                raise ValueError("synthetic publication clock rewind")
            raise OSError("synthetic file outage")

        monkeypatch.setattr("milo_home.reply_worker.publish_receipt", unavailable)
    with pytest.raises(ApiError, match="publication_pending"):
        tick(reply_case)
    run = latest(database)
    assert run["status"] == "running"
    with database.snapshot() as connection:
        assert (
            connection.execute(
                "SELECT fault_code FROM automatic_services WHERE kind='reply'"
            ).fetchone()[0]
            == "publication_pending"
        )
    assert tick(reply_case)["outcome"] == "service_disabled_or_faulted"
    with database.connect(write=True) as connection:
        connection.execute("DROP TRIGGER fail_publish")
    # A coordinator killed just after sealing may miss its normal credential cleanup.
    leftover = directory / run["id"] / "credential.env"
    leftover.write_text("synthetic abandoned credential")
    monkeypatch.setattr("milo_home.reply_recovery.unit_empty", lambda unit: True)
    assert replay(database, directory, owner, run["id"])["outcome"] == "succeeded"
    assert not leftover.exists()
    assert replay(database, directory, owner, run["id"])["outcome"] == "succeeded"
    assert len(reply_case[-1]) == 1
    assert client.get("/api/threads").json()[0]["pending_count"] == 0


def test_cancelled_sealed_result_can_be_closed_without_publication_or_relaunch(
    client, reply_case, monkeypatch
):
    from milo_home.reply_recovery import recover, request_stop

    send(client, reply_case)
    ready(reply_case)
    database, owner, _, _, directory, *_ = reply_case[0]

    def unavailable(*args, **kwargs):
        raise OSError("synthetic publication outage")

    monkeypatch.setattr("milo_home.reply_worker.publish_receipt", unavailable)
    with pytest.raises(ApiError, match="publication_pending"):
        tick(reply_case)
    run = latest(database)
    request_stop(database, owner, run["id"])
    monkeypatch.setattr("milo_home.reply_recovery.unit_empty", lambda unit: True)
    assert replay(database, directory, owner, run["id"])["outcome"] == "publication_blocked"
    assert (
        recover(database, directory, owner, run["id"])["outcome"] == "reconciled_without_relaunch"
    )
    assert latest(database)["status"] == "cancelled"
    reply_case[2].resume("reply", owner)
    assert len(reply_case[-1]) == 1
    assert client.get("/api/threads").json()[0]["pending_count"] == 1


def test_disabling_new_claims_does_not_cancel_inflight_reply(client, reply_case, monkeypatch):
    send(client, reply_case)
    ready(reply_case)
    owner = reply_case[0][1]

    def execute(command, prompt, store, run_id):
        store.started(run_id)
        reply_case[2].disable("reply", owner)
        events = EventStream()
        events.completed = 1
        return 0, events, True

    monkeypatch.setattr("milo_home.worker.execute", execute)
    assert tick(reply_case)["outcome"] == "succeeded"
    assert tick(reply_case)["outcome"] == "service_disabled_or_faulted"


def test_explicit_cancel_after_seal_prevents_publication(client, reply_case, monkeypatch):
    from milo_home.runs import RunStore

    send(client, reply_case)
    ready(reply_case)

    def cancel(database, owner, run_id, *, clock):
        RunStore(database, clock).stop(run_id)
        return publish_receipt(database, owner, run_id, clock=clock)

    monkeypatch.setattr("milo_home.reply_worker.publish_receipt", cancel)
    assert tick(reply_case)["outcome"] == "cancelled"
    assert client.get("/api/threads").json()[0]["pending_count"] == 1


def test_explicit_bad_credential_never_uses_shell_fallback(reply_case, monkeypatch):
    monkeypatch.setenv("MILO_SYNTHETIC_KEY", "must-not-be-used")
    reply_case[0][-1].write_text("bad\nmultiline")
    with pytest.raises(ValueError, match="configuration or credential"):
        tick(reply_case)
    assert reply_case[-1] == []
    assert tick(reply_case)["outcome"] == "service_disabled_or_faulted"


def test_http_activity_uses_current_service_evidence(client, reply_case):
    database, _, thread, *_ = reply_case[0]
    assert tick(reply_case)["outcome"] == "waiting_or_other_entry_first"
    response = client.get(f"/api/conversations/{thread}/activity")
    assert response.status_code == 200
    # The test API has a real clock; align only the observer time for this HTTP check.
    import time

    with database.connect(write=True) as connection:
        connection.execute(
            "UPDATE automatic_services SET checked_at=? WHERE kind='reply'", (int(time.time()),)
        )
    assert (
        client.get(f"/api/conversations/{thread}/activity").json()["reply_service"]["state"]
        == "scheduled"
    )
    reply_case[2].fault("reply", reply_case[0][1], "execution_failed")
    assert (
        client.get(f"/api/conversations/{thread}/activity").json()["reply_service"]["state"]
        == "faulted"
    )


def test_orphaned_reservation_is_not_recovered_by_the_next_tick(client, reply_case, monkeypatch):
    from milo_home.dispatch import DispatchStore

    send(client, reply_case)
    ready(reply_case)
    database, owner, *_ = reply_case[0]
    ticket = DispatchStore(database, lambda: reply_case[1][0], scope=ReplyScope(owner)).reserve()
    assert ticket is not None
    monkeypatch.setattr("milo_home.worker.stop_unit", lambda unit: pytest.fail("No timer recovery"))
    assert tick(reply_case)["outcome"] == "needs_reconciliation"
    assert reply_case[-1] == []
    assert latest(database)["status"] == "queued"


def test_process_stop_signal_is_honoured_before_sealing(client, reply_case, monkeypatch):
    send(client, reply_case)
    ready(reply_case)
    stop = StopRequest()

    def execute(command, prompt, store, run_id):
        store.started(run_id)
        stop.set()
        events = EventStream()
        events.completed = 1
        return 0, events, True

    monkeypatch.setattr("milo_home.worker.execute", execute)
    assert tick(reply_case, stop=stop)["outcome"] == "cancelled"
    assert client.get("/api/threads").json()[0]["pending_count"] == 1

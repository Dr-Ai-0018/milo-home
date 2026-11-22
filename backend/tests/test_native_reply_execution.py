import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from test_reply_execution import latest, ready, send
from test_reply_readiness import preparation as preparation
from test_work_process import fake_units as fake_units

from milo_home.automatic_services import AutomaticServices
from milo_home.codex_app_server import ThreadBinding
from milo_home.codex_config import CodexRuntime, NewawzConfig, private_create
from milo_home.codex_journal import PrivateJournal
from milo_home.conversations import ConversationService
from milo_home.errors import ApiError
from milo_home.execution_lock import execution_lock
from milo_home.native_messages import NativeMessageStore
from milo_home.native_policy import NativePolicyStore
from milo_home.reply_execution import run_once
from milo_home.reply_readiness import check_reply_service
from milo_home.reply_recovery import replay
from milo_home.scheduler_cli import StopRequest
from milo_home.work_access import WorkAccess
from milo_home.work_runtime import WorkRuntime
from milo_home.work_tasks import WorkTasks

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Native reply runtime requires root")


@pytest.fixture
def native_reply_case(preparation, monkeypatch, fake_units, request):
    database, owner, _, schedule, directory, profile, _ = preparation
    now = [int(schedule.clock())]
    profile.write_text(
        profile.read_text()
        .replace('"synthetic"', '"synthetic-provider"')
        .replace("[model_providers.synthetic]", "[model_providers.synthetic-provider]")
    )
    config = NewawzConfig.load(profile)
    home, workspace = directory / "native", directory / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    config.materialize(home)
    journal = PrivateJournal(home)
    installed = CodexRuntime(Path("/synthetic/codex"), Path("/synthetic/codex-code-mode-host"))
    journal.bind(
        ThreadBinding(
            getattr(request, "param", "synthetic-main"), config.provider, config.model, workspace
        ),
        config,
        installed,
    )
    private_create(home / "AGENTS.md", b"Synthetic home instructions.\n")
    access = WorkAccess(database, lambda: now[0])
    workspace_id = str(uuid4())
    access.register_workspace(workspace_id, "合成工作桌", workspace)
    access.grant(owner)
    session, _ = WorkTasks(database, lambda: now[0]).main_session(owner, workspace_id)
    with execution_lock(database) as lease:
        WorkRuntime(database, journal, lambda: now[0]).bind(owner, session["id"], lease=lease)
        NativePolicyStore(journal).configure(
            owner=owner,
            session=session["id"],
            model="gpt-6.2-sol",
            expected=None,
            now=now[0],
        )
    services = AutomaticServices(database, lambda: now[0])
    services.configure("work", owner, "milo-home-work.timer", session=session["id"])
    services.configure("reply", owner, "milo-home-reply.timer", enabled=True)
    output, scenario = directory / "requests.ndjson", ["auto-reply"]
    peer = Path(__file__).parent / "fixtures/codex_app_server.py"
    monkeypatch.setattr(CodexRuntime, "verify", lambda _: None)
    monkeypatch.setattr(
        CodexRuntime,
        "command",
        lambda _, *, workspace: [
            sys.executable,
            str(peer),
            scenario[0],
            str(output),
        ],
    )
    monkeypatch.setattr("milo_home.native_reply_process.DirectUnit", fake_units[0])
    monkeypatch.setattr("milo_home.codex_process.time.time", lambda: now[0])
    monkeypatch.setattr("milo_home.reply_execution.timer_active", lambda _: True)
    return (preparation, now, services, []), journal, output, scenario, fake_units


def tick(case, *, stop=None, **kwargs):
    old, journal, *_ = case
    database, owner, _, _, directory, profile, key = old[0]
    return run_once(
        database,
        directory,
        owner,
        profile,
        key,
        3600,
        stop or StopRequest(),
        clock=lambda: old[1][0],
        native_home=journal.path.parent,
        **kwargs,
    )


def requests(case):
    return [json.loads(line) for line in case[2].read_text().splitlines()]


def test_native_empty_queue_has_no_process_or_reservation(native_reply_case):
    assert tick(native_reply_case)["outcome"] == "waiting_or_other_entry_first"
    assert not native_reply_case[2].exists()
    assert not native_reply_case[1].outstanding()


def test_native_capacity_refuses_before_reserving_run_or_launching(native_reply_case, client):
    case, journal, output, *_ = native_reply_case
    database, owner, thread, *_ = case[0]
    send(client, case)
    ready(case)
    with journal._connect() as connection:
        for number in range(1, 5):
            connection.execute(
                "INSERT INTO streams VALUES (?,?,'closed',?)",
                (f"{number:032x}", f"milo-work-{number:032x}.service", 7 * 1024 * 1024),
            )
    with database.snapshot() as connection:
        before = connection.execute("SELECT count(*) FROM runs").fetchone()[0]
        permits = connection.execute("SELECT count(*) FROM execution_permits").fetchone()[0]
    assert tick(native_reply_case)["outcome"] == "native_storage_capacity"
    assert not output.exists()
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == before
        assert connection.execute("SELECT count(*) FROM execution_permits").fetchone()[0] == permits


def test_full_message_ledger_after_send_keeps_unknown_without_publishing(
    native_reply_case, client, monkeypatch
):
    case, journal, *_ = native_reply_case
    database, owner, *_ = case[0]
    send(client, case)
    ready(case)
    record = NativeMessageStore.record

    def full_on_accept(store, scope, event, *, lease):
        if event.kind == "accepted":
            raise sqlite3.OperationalError("database or disk is full")
        return record(store, scope, event, lease=lease)

    monkeypatch.setattr(NativeMessageStore, "record", full_on_accept)
    with pytest.raises(ApiError, match="needs_reconciliation"):
        tick(native_reply_case)
    run = latest(database)
    saved = NativeMessageStore(journal).inspect(run["id"])
    assert saved[1][0].kind == "sent"
    assert all(event.kind != "accepted" for event in saved[1])
    assert journal.outstanding()[0]["delivery"] == "unknown"
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM reply_execution_results").fetchone()[0] == 0
    assert tick(native_reply_case)["outcome"] == "service_disabled_or_faulted"


def test_native_preparation_does_not_hide_unknown_behind_capacity(native_reply_case):
    case, journal, *_ = native_reply_case
    database, owner, _, _, _, profile, key = case[0]
    stream = journal.open_stream("milo-reply-" + str(uuid4()) + ".service")
    journal.record(stream, {"kind": "intent", "request": {"id": 1, "method": "thread/read"}})
    journal.record(stream, {"kind": "unknown", "id": 1})
    with journal._connect() as connection:
        connection.execute("UPDATE streams SET bytes=? WHERE id=?", (32 * 1024 * 1024, stream))
    report = check_reply_service(
        database,
        owner,
        profile,
        key,
        clock=lambda: case[1][0],
        native_home=journal.path.parent,
        model="gpt-6.2-sol",
    )
    assert "native_reconciliation_required" in report["blockers"]
    assert "native_storage_capacity" not in report["blockers"]


def test_native_preparation_is_read_only_and_requires_the_registered_model(native_reply_case):
    case, journal, *_ = native_reply_case
    database, owner, _, _, _, profile, key = case[0]
    before = {p: p.read_bytes() for p in journal.path.parent.iterdir() if p.is_file()}
    report = check_reply_service(
        database,
        owner,
        profile,
        key,
        clock=lambda: case[1][0],
        native_home=journal.path.parent,
        model="gpt-6.2-sol",
    )
    assert report["ready"] and report["execution_available"] is False
    wrong = check_reply_service(
        database,
        owner,
        profile,
        key,
        clock=lambda: case[1][0],
        native_home=journal.path.parent,
        model="other-model",
    )
    assert wrong["blockers"] == ["native_configuration_invalid"]
    assert before == {p: p.read_bytes() for p in journal.path.parent.iterdir() if p.is_file()}
    assert not native_reply_case[2].exists()


def test_native_service_uses_original_reply_family_with_real_systemd(
    client, native_reply_case, monkeypatch
):
    from milo_home.codex_process import DirectUnit

    monkeypatch.setattr("milo_home.native_reply_process.DirectUnit", DirectUnit)
    case, journal, *_ = native_reply_case
    send(client, case)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    run = latest(case[0][0])
    scope, events = NativeMessageStore(journal).inspect(run["id"])
    assert scope.native_thread_id == "synthetic-main" and events[-1].stop_confirmed is True
    with journal._connect() as connection:
        stream = connection.execute("SELECT unit,state FROM streams").fetchone()
    assert stream[0] == run["unit_name"] + ".service" and stream[1] == "closed"
    assert not journal.outstanding() and not list(journal.path.parent.glob("*.env"))


def test_native_batches_resume_same_main_and_return_to_each_original_topic(
    client, native_reply_case
):
    case, journal, _, _, _ = native_reply_case
    database, owner, thread, *_ = case[0]
    send(client, case, 12)
    chat = ConversationService(database).chat(owner, thread)
    send(client, case, 2, conversation=chat["id"])
    ready(case)
    for _ in range(3):
        assert tick(native_reply_case)["outcome"] == "succeeded"
    recorded = requests(native_reply_case)
    starts = [r["params"] for r in recorded if r["method"] == "turn/start"]
    assert len(starts) == 3
    assert not any(r["method"] == "thread/start" for r in recorded)
    assert all(p["threadId"] == "synthetic-main" for p in starts)
    snapshots = [json.loads(p["input"][0]["text"].rsplit("\n", 1)[-1]) for p in starts]
    assert [len(s["selected"]) for s in snapshots] == [10, 2, 2]
    assert all("history" not in s for s in snapshots)
    assert snapshots[-1]["conversation_id"] == chat["id"]
    with database.snapshot() as connection:
        runs = [row[0] for row in connection.execute("SELECT id FROM runs")]
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
        assert connection.execute("SELECT count(*) FROM work_tasks").fetchone()[0] == 0
    for run in runs:
        scope, events = NativeMessageStore(journal).inspect(run)
        assert scope.native_thread_id == "synthetic-main"
        assert [e.kind for e in events] == ["sent", "accepted", "terminal", "stopped"]
        assert events[-1].stop_confirmed is True
    assert not journal.outstanding() and not list(journal.path.parent.glob("*.env"))


@pytest.mark.parametrize("scenario", ["auto-reply-invalid", "auto-reply-sensitive"])
def test_native_bad_final_is_not_published_or_retried(client, native_reply_case, scenario):
    case, journal, _, choice, _ = native_reply_case
    choice[0] = scenario
    send(client, case)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "failed"
    assert latest(case[0][0])["failure_code"] == "invalid_result"
    assert tick(native_reply_case)["outcome"] == "service_disabled_or_faulted"
    assert sum(r["method"] == "turn/start" for r in requests(native_reply_case)) == 1
    assert client.get("/api/threads").json()[0]["pending_count"] == 1
    assert not journal.outstanding()


def test_native_publication_failure_replays_sealed_result_without_new_turn(
    client, native_reply_case, monkeypatch
):
    case = native_reply_case[0]
    database, owner, _, _, directory, *_ = case[0]
    send(client, case)
    ready(case)
    with database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_publish BEFORE INSERT ON messages WHEN NEW.role='milo' "
            "BEGIN SELECT RAISE(ABORT,'synthetic outage'); END"
        )
    with pytest.raises(ApiError, match="publication_pending"):
        tick(native_reply_case)
    run = latest(database)
    assert run["status"] == "running"
    count = len(requests(native_reply_case))
    with database.connect(write=True) as connection:
        connection.execute("DROP TRIGGER fail_publish")
    monkeypatch.setattr("milo_home.reply_recovery.unit_empty", lambda _: True)
    monkeypatch.setattr("milo_home.native_reply_recovery.stop_family", lambda _: True)
    assert replay(database, directory, owner, run["id"])["outcome"] == "succeeded"
    assert replay(database, directory, owner, run["id"])["outcome"] == "succeeded"
    assert len(requests(native_reply_case)) == count


def test_native_lost_acceptance_retains_occupancy_and_does_not_retry(
    client, native_reply_case, monkeypatch
):
    from milo_home.codex_rpc import StdioRpc

    native_reply_case[3][0] = "auto-reply-lost"
    case, journal, *_ = native_reply_case
    send(client, case)
    ready(case)

    def limited(*args, **kwargs):
        kwargs["deadline"] = time.monotonic() + 0.5
        return StdioRpc(*args, **kwargs)

    monkeypatch.setattr("milo_home.native_reply_process.StdioRpc", limited)
    with pytest.raises(ApiError, match="needs_reconciliation"):
        tick(native_reply_case)
    run = latest(case[0][0])
    assert run["status"] == "stopping"
    assert tick(native_reply_case)["outcome"] == "service_disabled_or_faulted"
    assert sum(r["method"] == "turn/start" for r in requests(native_reply_case)) == 1
    assert journal.outstanding()
    assert not list(journal.path.parent.glob("*.env"))


def test_native_policy_model_mismatch_has_no_legacy_fallback(native_reply_case, monkeypatch):
    monkeypatch.setattr(
        "milo_home.reply_worker.prepare", lambda *a, **k: pytest.fail("No exec fallback")
    )
    with pytest.raises(ValueError, match="model differs"):
        tick(native_reply_case, model="different-model")
    assert not native_reply_case[2].exists()


def test_native_stop_after_acceptance_cancels_before_any_publication(
    client, native_reply_case, monkeypatch
):
    from milo_home.native_reply_delivery import NativeReplyDelivery

    stop = StopRequest()
    case = native_reply_case[0]
    native_reply_case[3][0] = "auto-reply-wait"
    send(client, case)
    ready(case)
    record = NativeReplyDelivery.record

    def stop_after_acceptance(self, event):
        record(self, event)
        if event["kind"] == "response" and event["id"] == self.request_id:
            stop.set()

    monkeypatch.setattr(NativeReplyDelivery, "record", stop_after_acceptance)
    assert tick(native_reply_case, stop=stop)["outcome"] == "cancelled"
    assert not native_reply_case[1].outstanding()
    assert client.get("/api/threads").json()[0]["pending_count"] == 1

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_runtime import bind
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import DeliveryUnknown, ProtocolError, StdioRpc
from milo_home.errors import ApiError
from milo_home.execution_lock import execution_lock
from milo_home.work_coordinator import run_inputs
from milo_home.work_delivery import WorkDelivery
from milo_home.work_prompt import PREAMBLE

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Host lease requires root")


@pytest.fixture
def delivery_case(runtime_case, tmp_path, request):
    case, runtime, journal = runtime_case
    work = case[0]
    work[5].clock = runtime.clock
    if hasattr(request, "param"):
        task, _ = work[5].create(work[1], work[-1]["id"], request.param, str(uuid4()))
        window = case[1].reserve(work[1], task["id"], 60)
    else:
        task, window = reserve(case)
    clients = []
    with execution_lock(work[0]) as lease:
        bind(runtime_case, lease)
        intent = runtime.claim_start(work[1], window["id"], lease=lease)

        def open_(scenario="normal", *, steer_supported=False):
            output = tmp_path / (scenario + ".ndjson")
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).parent / "fixtures/codex_app_server.py"),
                    scenario,
                    str(output),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )

            def stop():
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
                return True

            rpc = StdioRpc(
                process,
                deadline=time.monotonic() + 5,
                record=journal.recorder(intent.stream_id),
                stop=stop,
                checkpoint=lambda: None,
            )
            clients.append(rpc)
            delivery = WorkDelivery(
                runtime, work[1], intent, lease=lease, rpc=rpc, steer_supported=steer_supported
            )
            delivery.resume()
            return delivery, rpc, output

        yield case, runtime, journal, lease, task, window, open_
        for rpc in clients:
            rpc.close()


def rows(case):
    with case[0][0].connect() as connection:
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ["work_inputs", "work_tasks", "execution_windows", "execution_permits"]
        }


def entries(journal):
    with journal._connect() as connection:
        return [json.loads(row[0]) for row in connection.execute("SELECT payload FROM entries")]


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("steer-accepted", "acknowledged"),
        ("steer-conflict", "conflict"),
        ("steer-lost", "unknown"),
        ("steer-wrong-turn", "unknown"),
    ],
)
def test_same_turn_supplement_never_gets_a_second_launch(delivery_case, scenario, expected):
    case, runtime, journal, lease, task, _, open_ = delivery_case
    _, owner, _, _, _, tasks, *_ = case[0]
    delivery, rpc, output = open_(scenario, steer_supported=True)
    start = delivery.start
    key = str(uuid4())
    active_turn = None

    def supplement(input_id):
        nonlocal active_turn
        turn = start(input_id)
        active_turn = tasks.get(owner, task["id"])["active_turn"]
        assert active_turn
        saved, created = tasks.append(owner, task["id"], "这轮加一句", key, active_turn)
        assert created and saved["delivery"] == "queued"
        assert saved["active_turn"] == active_turn
        with pytest.raises(ApiError):
            tasks.append(owner, task["id"], "这轮加一句", key, str(uuid4()))
        tasks.append(owner, task["id"], "来不及发的第二段", str(uuid4()), active_turn)
        return turn

    delivery.start = supplement
    rpc.deadline = time.monotonic() + 1
    if expected == "unknown":
        with pytest.raises(DeliveryUnknown):
            run_inputs(runtime, owner, delivery.intent, lease=lease, delivery=delivery)
    else:
        result = run_inputs(runtime, owner, delivery.intent, lease=lease, delivery=delivery)
        assert (result.outcome, result.turns) == ("completed", 1)
    saved, created = tasks.append(owner, task["id"], "这轮加一句", key, active_turn)
    assert not created and saved["delivery"] == expected
    inputs = tasks.inputs(owner, task["id"], 0, 20)["inputs"]
    assert [i["delivery"] for i in inputs] == ["acknowledged", expected, "cancelled"]
    assert tasks.get(owner, task["id"])["active_turn"] is None
    with pytest.raises(ApiError):
        tasks.append(owner, task["id"], "错过这一轮", str(uuid4()), active_turn)
    assert len(rows(case)["execution_permits"]) == 1
    requests = [json.loads(line) for line in output.read_text().splitlines()]
    turns = [r for r in requests if r["method"].startswith("turn/")]
    assert [r["method"] for r in turns] == ["turn/start", "turn/steer"]
    assert turns[1]["params"]["expectedTurnId"] == "turn-1"
    assert turns[1]["params"]["clientUserMessageId"] == saved["id"]
    evidence = next(e["work_steer"] for e in entries(journal) if "work_steer" in e)
    assert evidence["permit_id"] == active_turn


@pytest.mark.parametrize("change", ["stop", "revoke"])
def test_supplement_loses_to_stop_or_revocation(delivery_case, change):
    case, _, _, _, task, _, open_ = delivery_case
    _, owner, _, _, access, tasks, *_ = case[0]
    delivery, _, output = open_("steer-accepted", steer_supported=True)
    delivery.start(task["initial_input"]["id"])
    active_turn = tasks.get(owner, task["id"])["active_turn"]
    tasks.append(owner, task["id"], "这轮加一句", str(uuid4()), active_turn)
    if change == "stop":
        tasks.stop(owner, task["id"], str(uuid4()))
    else:
        access.revoke(owner)
    with pytest.raises(ApiError):
        delivery.wait()
    assert rows(case)["work_inputs"][1]["delivery"] == "cancelled"
    assert '"method": "turn/steer"' not in output.read_text()


@pytest.mark.parametrize("scenario,supported", [("normal", True), ("interrupt", False)])
def test_supplements_require_a_supported_still_active_turn(delivery_case, scenario, supported):
    case, _, _, _, task, _, open_ = delivery_case
    _, owner, _, _, _, tasks, *_ = case[0]
    delivery, _, output = open_(scenario, steer_supported=supported)
    delivery.start(task["initial_input"]["id"])
    assert tasks.get(owner, task["id"])["active_turn"] is None
    with pytest.raises(ApiError):
        tasks.append(owner, task["id"], "不应收件", str(uuid4()), delivery.permit["id"])
    assert len(rows(case)["work_inputs"]) == 1
    assert '"method": "turn/steer"' not in output.read_text()


def test_ack_terminal_and_family_cleanup_are_distinct(delivery_case):
    case, _, journal, _, task, window, open_ = delivery_case
    delivery, rpc, output = open_()
    input_id = task["initial_input"]["id"]
    assert delivery.start(input_id) == "turn-1"
    state = rows(case)
    assert state["work_inputs"][0]["delivery"] == "acknowledged"
    assert state["execution_permits"][0]["state"] == "launched"
    assert state["work_tasks"][0]["status"] == "running"
    assert state["execution_windows"][0]["state"] == "running"
    assert delivery.wait() == "completed"
    state = rows(case)
    assert state["execution_permits"][0]["state"] == "finished"
    # Terminal can precede the start response. Neither event releases occupancy.
    assert state["execution_windows"][0]["state"] == "running"
    assert state["work_tasks"][0]["status"] == "running"
    with pytest.raises(ProtocolError):
        delivery.start(input_id)
    with pytest.raises(ProtocolError):
        delivery.wait()
    requests = [json.loads(line) for line in output.read_text().splitlines()]
    starts = [r for r in requests if r["method"] == "turn/start"]
    assert len(starts) == 1
    data = json.loads(starts[0]["params"]["input"][0]["text"].removeprefix(PREAMBLE))
    assert data["input"]["body"] == task["initial_input"]["body"]
    assert data["actor"]["id"] == case[0][1]
    assert data["task"]["id"] == task["id"]
    assert data["input"]["delivery_at_read"] == "queued"
    intent = next(e for e in entries(journal) if e.get("work"))
    assert intent["request"]["id"] == delivery.request_id
    assert intent["work"] == {
        "input_id": input_id,
        "permit_id": delivery.permit["id"],
        "window_id": window["id"],
        "task_id": task["id"],
        "stream_id": delivery.intent.stream_id,
    }
    assert rpc.close()
    assert journal.close_stream(delivery.intent.stream_id, stop=lambda: True)
    assert case[1].close(window["id"], stop=lambda: True, delivery_known=True, outcome="completed")


@pytest.mark.parametrize("scenario", ["eof", "lost", "malformed"])
def test_lost_delivery_is_unknown_and_never_replayed(delivery_case, scenario):
    case, _, journal, _, task, window, open_ = delivery_case
    delivery, rpc, output = open_(scenario)
    rpc.deadline = time.monotonic() + 0.2
    with pytest.raises(DeliveryUnknown):
        delivery.start(task["initial_input"]["id"])
    state = rows(case)
    assert state["work_inputs"][0]["delivery"] == "unknown"
    assert state["execution_permits"][0]["state"] == "unknown"
    assert state["execution_windows"][0]["state"] == "unknown"
    assert state["work_tasks"][0]["status"] == "needs_reconciliation"
    assert PrivateJournal(journal.path.parent).outstanding()[0]["delivery"] == "unknown"
    with pytest.raises(ProtocolError):
        delivery.start(task["initial_input"]["id"])
    assert output.read_text().count('"method": "turn/start"') == 1
    assert rpc.close()
    assert not case[1].close(window["id"], stop=lambda: True, delivery_known=True, outcome="failed")


@pytest.mark.parametrize("stage", ["intent", "response", "acknowledgement"])
def test_storage_failure_is_not_a_success_or_permission_to_resend(
    delivery_case, monkeypatch, stage
):
    case, _, journal, _, task, _, open_ = delivery_case
    delivery, _, output = open_()
    original = journal.record

    def fail(stream, event):
        if stage == "intent" and event.get("work"):
            assert rows(case)["work_inputs"][0]["delivery"] == "sending"
            raise OSError("synthetic intent storage failure")
        if stage == "response" and event["kind"] == "response":
            raise OSError("synthetic response storage failure")
        original(stream, event)

    monkeypatch.setattr(journal, "record", fail)
    if stage == "acknowledgement":

        def fail_ack():
            raise OSError("synthetic business acknowledgement failure")

        monkeypatch.setattr(delivery, "_acknowledge", fail_ack)
    with pytest.raises((OSError, DeliveryUnknown)):
        delivery.start(task["initial_input"]["id"])
    state = rows(case)
    assert state["execution_permits"][0]["state"] == "unknown"
    assert state["work_inputs"][0]["delivery"] == "unknown"
    assert state["execution_windows"][0]["state"] == "unknown"
    assert output.read_text().count('"method": "turn/start"') == (0 if stage == "intent" else 1)


@pytest.mark.parametrize("change", ["revoke", "pause", "stop", "deadline", "rewind"])
def test_change_after_prepare_prevents_the_pipe_write(delivery_case, monkeypatch, change):
    case, _, _, _, task, window, open_ = delivery_case
    delivery, _, output = open_()
    original = delivery._prepare

    def prepare(input_id):
        original(input_id)
        if change == "revoke":
            case[0][4].revoke(case[0][1])
        elif change == "pause":
            case[2].pause(True)
        elif change == "stop":
            case[0][5].stop(case[0][1], task["id"], str(uuid4()))
        elif change == "deadline":
            case[-1][0] = window["deadline_at"]
        else:
            case[-1][0] = window["created_at"] - 1

    monkeypatch.setattr(delivery, "_prepare", prepare)
    with pytest.raises(ApiError):
        delivery.start(task["initial_input"]["id"])
    state = rows(case)
    assert state["execution_permits"][0]["state"] == "cancelled"
    assert '"method": "turn/start"' not in output.read_text()
    assert state["work_inputs"][0]["delivery"] in {"queued", "cancelled"}


def test_authority_is_rechecked_after_intent_before_write(delivery_case, monkeypatch):
    case, _, journal, _, task, _, open_ = delivery_case
    delivery, _, output = open_()
    original = journal.record

    def revoke(stream, event):
        original(stream, event)
        if event.get("work"):
            case[0][4].revoke(case[0][1])

    monkeypatch.setattr(journal, "record", revoke)
    with pytest.raises(DeliveryUnknown):
        delivery.start(task["initial_input"]["id"])
    state = rows(case)
    assert state["work_inputs"][0]["delivery"] == "unknown"
    assert state["execution_windows"][0]["state"] == "unknown"
    assert '"method": "turn/start"' not in output.read_text()


def test_unknown_outcome_preserves_a_real_acknowledgement(delivery_case):
    case, _, _, _, task, _, open_ = delivery_case
    delivery, rpc, _ = open_("interrupt")
    delivery.start(task["initial_input"]["id"])
    rpc.deadline = time.monotonic() + 0.05
    with pytest.raises(ProtocolError):
        delivery.wait()
    state = rows(case)
    assert state["work_inputs"][0]["delivery"] == "acknowledged"
    assert state["execution_permits"][0]["state"] == "unknown"
    assert state["work_tasks"][0]["status"] == "needs_reconciliation"


@pytest.mark.parametrize("scope", ["another-task", "out-of-order"])
def test_other_task_and_out_of_order_supplement_cannot_be_sent(delivery_case, scope):
    case, _, _, _, task, _, open_ = delivery_case
    work = case[0]
    delivery, _, output = open_()
    if scope == "another-task":
        other = create(work)
        with pytest.raises(ProtocolError, match="Saved input"):
            delivery.start(other["initial_input"]["id"])
        assert rows(case)["execution_permits"] == []
    else:
        supplement, _ = work[5].append(work[1], task["id"], "合成补充", str(uuid4()))
        with pytest.raises(ProtocolError, match="not authorized"):
            delivery.start(supplement["id"])
        assert rows(case)["execution_permits"][0]["state"] == "cancelled"
    assert '"method": "turn/start"' not in output.read_text()


def test_claim_input_and_consume_permit_roll_back_together(delivery_case):
    case, _, _, _, task, _, open_ = delivery_case
    delivery, _, output = open_()
    with case[0][0].connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER synthetic_consume_failure BEFORE UPDATE OF state "
            "ON execution_permits WHEN NEW.state='launched' "
            "BEGIN SELECT RAISE(ABORT,'synthetic consume failure'); END"
        )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match="synthetic consume"):
        delivery.start(task["initial_input"]["id"])
    state = rows(case)
    assert state["work_inputs"][0]["delivery"] == "queued"
    assert state["execution_windows"][0]["state"] == "held"
    assert state["work_tasks"][0]["status"] == "queued"
    assert state["execution_permits"][0]["state"] == "cancelled"
    assert '"method": "turn/start"' not in output.read_text()


def test_changed_protocol_body_cannot_use_an_input_permit(delivery_case, monkeypatch):
    case, _, _, _, task, _, open_ = delivery_case
    delivery, _, output = open_()
    start = delivery.conversation.start
    monkeypatch.setattr(
        delivery.conversation, "start", lambda text, **kwargs: start("伪造协议正文", **kwargs)
    )
    with pytest.raises(ProtocolError, match="does not match"):
        delivery.start(task["initial_input"]["id"])
    assert rows(case)["work_inputs"][0]["delivery"] == "unknown"
    assert '"method": "turn/start"' not in output.read_text()


@pytest.mark.parametrize("delivery_case", ["字" * 4000], indirect=True, ids=["4000-chars"])
def test_full_body_plus_metadata_reaches_the_private_protocol(delivery_case):
    _, _, _, _, task, _, open_ = delivery_case
    delivery, _, output = open_()
    delivery.start(task["initial_input"]["id"])
    assert delivery.wait() == "completed"
    request = next(
        json.loads(line)
        for line in output.read_text().splitlines()
        if json.loads(line)["method"] == "turn/start"
    )
    text = request["params"]["input"][0]["text"]
    assert len(text) > 4000
    assert json.loads(text.removeprefix(PREAMBLE))["input"]["body"] == "字" * 4000

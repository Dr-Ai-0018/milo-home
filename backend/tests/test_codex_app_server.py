import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from milo_home.codex_app_server import MainConversation, ThreadBinding
from milo_home.codex_execution_policy import NativeExecutionPolicy
from milo_home.codex_rpc import DeliveryUnknown, ProtocolError, RequestRejected, StdioRpc


@pytest.fixture
def peer(tmp_path):
    clients = []

    def create(
        scenario="normal",
        *,
        stop_confirmed=True,
        permit=True,
        input_limit=4000,
        execution_policy=NativeExecutionPolicy.LEGACY_DIRECT,
    ):
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
        records = []
        launches = []

        def stop():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
            return stop_confirmed

        def authorize(input_id):
            launches.append(input_id)
            return permit

        rpc = StdioRpc(
            process,
            deadline=time.monotonic() + 5,
            record=records.append,
            stop=stop,
            checkpoint=lambda: None,
        )
        binding = ThreadBinding("main-thread", "synthetic-provider", "synthetic-model", tmp_path)
        session = MainConversation(
            rpc,
            binding,
            authorize_turn=authorize,
            execution_policy=execution_policy,
            input_limit=input_limit,
        )
        clients.append(rpc)
        return session, rpc, records, launches, output

    yield create
    for rpc in clients:
        rpc.close()


@pytest.mark.parametrize("scenario", ["normal", "split"])
def test_same_thread_incremental_turns_and_terminal_before_ack(peer, scenario):
    session, rpc, records, launches, output = peer(scenario)
    session.resume()
    session.start("第一封，只发送本轮", input_id="input-1")
    assert session.wait() == "completed"
    session.start("第二封增量", input_id="input-2")
    assert session.wait() == "completed"
    requests = [json.loads(line) for line in output.read_text().splitlines()]
    starts = [r["params"] for r in requests if r["method"] == "turn/start"]
    assert [r["input"][0]["text"] for r in starts] == ["第一封，只发送本轮", "第二封增量"]
    assert all(r["threadId"] == "main-thread" for r in starts)
    assert not any(r["method"] == "thread/start" for r in requests)
    assert launches == ["input-1", "input-2"]
    assert all("outputSchema" not in request for request in starts)
    assert any(r["kind"] == "notification" for r in records)
    assert not rpc.broken


def test_native_reply_schema_and_long_incremental_batch_keep_same_thread(peer):
    session, _, _, launches, output = peer(input_limit=65536)
    session.resume()
    from milo_home.reply_result import OUTPUT_SCHEMA

    text = "本轮新增" * 10000
    session.start(text, input_id="native-message-batch", output_schema=OUTPUT_SCHEMA)
    assert session.wait() == "completed"
    requests = [json.loads(line) for line in output.read_text().splitlines()]
    start = next(r["params"] for r in requests if r["method"] == "turn/start")
    assert start["outputSchema"] == OUTPUT_SCHEMA
    assert start["threadId"] == "main-thread" and start["input"][0]["text"] == text
    assert launches == ["native-message-batch"]


def test_auto_review_uses_same_thread_and_leaves_review_and_compaction_to_codex(peer):
    session, rpc, records, launches, output = peer(
        "auto-normal", execution_policy=NativeExecutionPolicy.AUTO_REVIEW
    )
    session.resume()
    for index in range(2):
        session.start(f"新增留言{index}", input_id=f"message-{index}")
        assert session.wait() == "completed"
    requests = [json.loads(line) for line in output.read_text().splitlines()]
    resume = next(r["params"] for r in requests if r["method"] == "thread/resume")
    assert resume["approvalsReviewer"] == "auto_review"
    assert resume["sandbox"] == "workspace-write"
    assert resume["approvalPolicy"] == "on-request"
    assert all(
        r["params"]["threadId"] == "main-thread"
        for r in requests
        if r["method"] in {"thread/resume", "turn/start"}
    )
    assert [r["method"] for r in requests].count("turn/start") == 2
    assert not any(r["method"] in {"thread/start", "thread/compact/start"} for r in requests)
    assert launches == ["message-0", "message-1"]
    assert sum(r["kind"] == "notification" for r in records) > 2
    assert not rpc.broken


@pytest.mark.parametrize(
    "scenario", ["auto-human", "auto-never", "auto-unrestricted", "auto-reviewer-missing"]
)
def test_auto_review_rejects_permission_or_reviewer_fallback_before_launch(peer, scenario):
    session, _, _, launches, _ = peer(scenario, execution_policy=NativeExecutionPolicy.AUTO_REVIEW)
    with pytest.raises(ProtocolError, match="configuration mismatch"):
        session.resume()
    assert session.state == "needs_reconciliation"
    assert launches == []


@pytest.mark.parametrize(
    "scenario", ["auto-foreign-review", "auto-foreign-strict", "auto-manual-request"]
)
def test_auto_review_never_handles_a_manual_approval_or_foreign_turn(peer, scenario):
    session, rpc, records, launches, output = peer(
        scenario, execution_policy=NativeExecutionPolicy.AUTO_REVIEW
    )
    session.resume()
    rpc.deadline = time.monotonic() + 0.2
    with pytest.raises(DeliveryUnknown):
        session.start("只投一次", input_id="message-1")
    assert records[-1]["kind"] == "unknown"
    assert session.state == "needs_reconciliation"
    assert launches == ["message-1"]
    requests = [json.loads(line) for line in output.read_text().splitlines()]
    assert not any("result" in request for request in requests)


@pytest.mark.parametrize("scenario", ["wrong-provider", "wrong-sandbox", "active"])
def test_resume_never_silently_accepts_fallback_or_active_thread(peer, scenario):
    session, _, _, launches, _ = peer(scenario)
    with pytest.raises(ProtocolError):
        session.resume()
    assert session.state == "needs_reconciliation"
    with pytest.raises(ProtocolError):
        session.start("不许执行", input_id="rejected")
    assert launches == []


@pytest.mark.parametrize(
    "scenario",
    ["lost", "eof", "malformed", "server-request", "unknown-item", "frame-limit", "stderr-limit"],
)
def test_uncertain_start_is_recorded_and_cannot_be_retried(peer, scenario):
    session, rpc, records, launches, output = peer(scenario)
    session.resume()
    rpc.deadline = time.monotonic() + 0.2
    with pytest.raises(DeliveryUnknown):
        session.start("可能已经产生副作用", input_id="input-1")
    assert session.state == "needs_reconciliation"
    assert records[-1]["kind"] == "unknown"
    with pytest.raises(ProtocolError):
        session.start("不能重放", input_id="input-1")
    requests = [json.loads(line) for line in output.read_text().splitlines()]
    assert sum(r["method"] == "turn/start" for r in requests) == 1
    assert launches == ["input-1"]


def test_steer_conflict_does_not_create_another_turn(peer):
    session, _, _, launches, output = peer("steer-conflict")
    session.resume()
    turn = session.start("开始", input_id="input-1")
    with pytest.raises(ProtocolError):
        session.steer("过期", input_id="input-2", expected_turn="old-turn")
    with pytest.raises(RequestRejected, match="^Codex rejected the request$"):
        session.steer("竞争", input_id="input-2", expected_turn=turn)
    assert session.wait() == "completed"
    requests = [json.loads(line) for line in output.read_text().splitlines()]
    assert sum(r["method"] == "turn/start" for r in requests) == 1
    assert launches == ["input-1"]


@pytest.mark.parametrize(
    ("scenario", "terminal"),
    [
        ("error-before-start", "failed"),
        ("error-then-failed", "failed"),
        ("error-retry", "completed"),
    ],
)
def test_scoped_provider_error_waits_for_original_terminal(peer, scenario, terminal):
    session, rpc, _, launches, _ = peer(scenario)
    session.resume()
    session.start("开始", input_id="input-1")
    assert session.wait() == terminal
    assert launches == ["input-1"] and not rpc.broken


@pytest.mark.parametrize("scenario", ["error-no-terminal", "error-foreign-turn"])
def test_provider_error_is_not_a_terminal_or_proof_for_another_turn(peer, scenario):
    session, _, _, launches, _ = peer(scenario)
    session.resume()
    # The bad event may share the same pipe read as the start response.
    with pytest.raises((ProtocolError, DeliveryUnknown)):
        session.start("开始", input_id="input-1")
        session.wait()
    assert session.state == "needs_reconciliation" and session.terminal is None
    with pytest.raises(ProtocolError):
        session.start("不能重放", input_id="input-1")
    assert launches == ["input-1"]


@pytest.mark.parametrize("confirmed", [True, False])
def test_interrupt_ack_never_substitutes_for_process_family_confirmation(peer, confirmed):
    session, rpc, _, _, _ = peer("interrupt", stop_confirmed=confirmed)
    session.resume()
    session.start("挂起", input_id="input-1")
    assert session.stop() is confirmed
    assert session.state == ("stopped" if confirmed else "needs_reconciliation")
    assert rpc.closed
    with pytest.raises(ProtocolError):
        session.start("不能继续", input_id="input-2")


def test_every_turn_requires_a_fresh_upper_layer_permit(peer):
    session, _, _, launches, output = peer(permit=False)
    session.resume()
    with pytest.raises(ProtocolError, match="not authorized"):
        session.start("拒绝启动", input_id="input-1")
    assert launches == ["input-1"]
    assert '"method": "turn/start"' not in output.read_text()


def test_intent_record_failure_prevents_sending(peer):
    session, rpc, _, _, output = peer()
    session.resume()

    def unavailable(_):
        raise OSError("private storage failure")

    rpc.record = unavailable
    with pytest.raises(OSError):
        session.start("不应发送", input_id="input-1")
    assert '"method": "turn/start"' not in output.read_text()
    assert session.state == "needs_reconciliation"


def test_cross_thread_start_is_rejected_before_reserving_or_sending(peer):
    session, _, _, launches, output = peer()
    session.resume()
    errors = []

    def start_from_wrong_writer():
        try:
            session.start("不能并发操作同一连接", input_id="input-1")
        except ProtocolError as error:
            errors.append(str(error))

    thread = threading.Thread(target=start_from_wrong_writer)
    thread.start()
    thread.join(timeout=2)
    assert errors == ["Connection belongs to another writer"]
    assert session.state == "idle" and launches == []
    assert '"method": "turn/start"' not in output.read_text()


def test_lost_durable_response_record_blocks_replay(peer):
    session, rpc, records, _, output = peer()
    session.resume()

    def fail_response_record(event):
        if event["kind"] == "response":
            raise OSError("storage unavailable after execution")
        records.append(event)

    rpc.record = fail_response_record
    with pytest.raises(DeliveryUnknown):
        session.start("可能已经完成", input_id="input-1")
    assert rpc.broken and session.state == "needs_reconciliation"
    assert records[-1]["kind"] == "unknown"
    assert output.read_text().count('"method": "turn/start"') == 1

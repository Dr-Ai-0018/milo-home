"""Verify native protocol evidence before using the existing reply publication gate."""

import hashlib

from .codex_rpc import ProtocolError
from .native_messages import NativeMessageStore
from .native_reply_input import native_input_digest, native_reply_result
from .reply_receipts import seal_confirmed_result
from .reply_results import output_version, published_bodies
from .work_output import _items


def stream_evidence(journal, stream_id):
    snapshot = journal.stream_snapshot(stream_id)
    acknowledged = all(row["state"] == "acknowledged" for row in snapshot["requests"])
    return snapshot["stream"], snapshot["entries"], acknowledged


def final_result(journal, stream_id, scope, turn_id, *, policy=None):
    _, entries, _ = stream_evidence(journal, stream_id)
    items, conflict = _items(entries, scope.native_thread_id, turn_id)
    finals = [
        item.get("text")
        for item in items
        if item["type"] == "agentMessage" and item.get("phase") == "final_answer"
    ]
    if conflict or len(finals) != 1 or not isinstance(finals[0], str):
        raise ValueError("Native reply requires exactly one unambiguous final answer")
    if len(finals[0].encode()) > 256 * 1024:
        raise ValueError("Native reply result exceeds the transport boundary")
    version = requested_output_version(entries, scope)
    result = native_reply_result(
        finals[0],
        [str(identity) for identity in scope.message_ids],
        version=version,
        revisit_source_ids=scope.revisit.source_ids if scope.revisit else (),
    )
    if policy is not None and not all(policy.permits(body) for body in published_bodies(result)):
        raise ValueError("Native reply result cannot be safely published")
    return result


def requested_output_version(entries, scope):
    intents = [
        event["request"]
        for _, event in entries
        if event.get("kind") == "intent"
        and event.get("request", {}).get("method") == "turn/start"
        and event["request"].get("params", {}).get("clientUserMessageId") == scope.input_id
    ]
    if len(intents) != 1:
        raise ProtocolError("Native reply output format has no unique original request")
    return output_version(intents[0]["params"].get("outputSchema"))


def result_digest(result):
    return hashlib.sha256(result.model_dump_json().encode()).hexdigest()


def seal_native_result(runtime, scope, result, *, lease):
    runtime._identity(lease)
    saved = NativeMessageStore(runtime.journal).inspect(scope.run_id)
    if saved is None or saved[0] != scope:
        raise ProtocolError("Native reply routing evidence is missing")
    events = saved[1]
    accepted = next((event for event in events if event.kind == "accepted"), None)
    terminal = next((event for event in events if event.kind == "terminal"), None)
    if (
        not events
        or accepted is None
        or terminal is None
        or terminal.outcome != "completed"
        or terminal.result_digest != result_digest(result)
        or events[-1].kind != "stopped"
        or events[-1].stop_confirmed is not True
    ):
        raise ProtocolError("Native reply requires a completed result and confirmed stop")
    stream, entries, acknowledged = stream_evidence(runtime.journal, terminal.stream_id)
    if (
        stream["state"] != "closed"
        or stream["unit"] != "milo-reply-" + str(scope.run_id) + ".service"
        or not acknowledged
    ):
        raise ProtocolError("Native reply execution still requires reconciliation")
    _require_protocol(entries, scope, accepted)
    if final_result(runtime.journal, terminal.stream_id, scope, accepted.turn_id) != result:
        raise ProtocolError("Native final answer differs from its recorded result")
    runtime._identity(lease)
    return seal_confirmed_result(runtime.database, str(scope.run_id), result, clock=runtime.clock)


def _require_protocol(entries, scope, accepted):
    intents = [
        event["request"]
        for _, event in entries
        if event.get("kind") == "intent"
        and event.get("request", {}).get("id") == accepted.request_id
    ]
    if len(intents) != 1 or intents[0].get("method") != "turn/start":
        raise ProtocolError("Native reply launch request is missing")
    params = intents[0]["params"]
    requested_output_version(entries, scope)
    input_ = params.get("input", [])
    if (
        params.get("threadId") != scope.native_thread_id
        or params.get("clientUserMessageId") != scope.input_id
        or len(input_) != 1
        or input_[0].get("type") != "text"
        or native_input_digest(input_[0].get("text", "")) != scope.input_digest
    ):
        raise ProtocolError("Native reply request differs from its saved inputs")
    responses = [
        event.get("response", {}).get("result", {}).get("turn", {})
        for _, event in entries
        if event.get("kind") == "response" and event.get("id") == accepted.request_id
    ]
    terminals = [
        event["message"]["params"]["turn"]
        for _, event in entries
        if event.get("kind") == "notification"
        and event.get("message", {}).get("method") == "turn/completed"
        and event["message"].get("params", {}).get("threadId") == scope.native_thread_id
        and event["message"]["params"].get("turn", {}).get("id") == accepted.turn_id
    ]
    stopped = any(
        event.get("kind") == "process" and event.get("phase") == "stop_confirmed"
        for _, event in entries
    )
    if (
        len(responses) != 1
        or responses[0].get("id") != accepted.turn_id
        or not terminals
        or any(turn.get("status") != "completed" for turn in terminals)
        or not stopped
    ):
        raise ProtocolError("Native reply acceptance, terminal or stop proof is incomplete")

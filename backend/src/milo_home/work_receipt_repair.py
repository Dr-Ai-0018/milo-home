"""Explicit fact-only repair from the original durable protocol, never a retry."""

from .codex_rpc import ProtocolError
from .work_access import require_task
from .work_native_failure import matching_failure, read_native_failure
from .work_receipts import OUTCOMES, validated_receipts
from .work_recovery import check_receipt_permits
from .work_stop import stop_shape


def repair_candidates(
    entries, *, stream_id, window_id, task_id, thread_id, observed_at, native=None
):
    scope = dict(stream_id=stream_id, window_id=window_id, task_id=task_id, thread_id=thread_id)
    known = validated_receipts(entries, **scope)
    seen = {r["work"]["input_id"] for _, r in known}
    responses = {}
    terminals = {}
    for sequence, event in entries:
        if event.get("kind") == "response":
            if event["id"] in responses:
                raise ProtocolError("Duplicate original response evidence")
            responses[event["id"]] = (sequence, event["response"])
        elif event.get("kind") == "notification":
            message = event["message"]
            params = message.get("params", {})
            if message.get("method") == "turn/completed" and params.get("threadId") == thread_id:
                turn = params.get("turn", {})
                terminals.setdefault(turn.get("id"), []).append((sequence, turn.get("status")))
    candidates, sources = [], []
    for sequence, event in entries:
        request = event.get("request", {})
        if event.get("kind") != "intent" or request.get("method") != "turn/start":
            continue
        work = event.get("work", {})
        if work.get("input_id") in seen:
            continue
        response_sequence, response = responses.get(request.get("id"), (None, {}))
        if response_sequence is None:
            continue
        turn = response.get("result", {}).get("turn", {})
        statuses = {status for _, status in terminals.get(turn.get("id"), [])}
        if turn.get("status") in OUTCOMES:
            statuses.add(turn["status"])
        native_failure = None
        if not statuses and native is not None and turn.get("status") == "inProgress":
            lines, digest = native
            error_sequence = matching_failure(lines, thread_id, turn.get("id"), entries, sequence)
            if error_sequence is not None:
                statuses.add("failed")
                native_failure = {"rollout_sha256": digest, "error_sequence": error_sequence}
        if not statuses:
            continue
        if (
            len(statuses) != 1
            or not statuses <= OUTCOMES
            or "error" in response
            or turn.get("status") not in OUTCOMES | {"inProgress"}
            or response_sequence <= sequence
            or any(s <= sequence for s, _ in terminals.get(turn.get("id"), []))
        ):
            raise ProtocolError("Conflicting original terminal evidence")
        receipt = {
            "kind": "work_terminal",
            "work": work,
            "request_id": request.get("id"),
            "turn_id": turn.get("id"),
            "outcome": next(iter(statuses)),
            "observed_at": observed_at,
        }
        if native_failure:
            receipt["native_failure"] = native_failure
        candidates.append(receipt)
        sources.append(
            {
                "input_id": work.get("input_id"),
                "request_sequence": sequence,
                "response_sequence": response_sequence,
                "terminal_sequences": [s for s, _ in terminals.get(turn.get("id"), [])],
            }
        )
    # Reuse the original exact request/input/permit/thread/turn checks. No output
    # is extracted: the original ephemeral credential checks cannot be repeated.
    last = entries[-1][0] if entries else 0
    validated_receipts(entries + [(last + i + 1, r) for i, r in enumerate(candidates)], **scope)
    return candidates, sources


def repair_after_stop(runtime, owner, window, private, proof, lease, *, native_rollout=None):
    """Atomically append repaired facts, their provenance, and this fresh stop."""
    stream = private["stream"]["id"]
    if any(r["state"] != "acknowledged" for r in private["requests"]):
        return runtime.journal.record(stream, proof)
    thread_id = runtime.journal.binding().thread_id
    native = (
        read_native_failure(runtime.journal.path.parent, native_rollout, thread_id)
        if native_rollout is not None
        else None
    )
    candidates, sources = repair_candidates(
        private["entries"],
        stream_id=stream,
        window_id=window["id"],
        task_id=window["task_id"],
        thread_id=thread_id,
        observed_at=proof["checked_at"],
        native=native,
    )
    if not candidates:
        return runtime.journal.record(stream, proof)
    with runtime.database.connect() as connection:
        connection.execute("BEGIN")
        require_task(connection, owner, window["task_id"])
        check_receipt_permits(connection, window["id"], [(0, r) for r in candidates])
    runtime._identity(lease)
    marker = {
        "kind": "work_receipt_repair",
        "source": "original_protocol_and_native_rollout" if native else "original_protocol",
        "checked_at": proof["checked_at"],
        "evidence": sources,
    }
    stop_shape(proof)
    return runtime.journal.append_repair(private, candidates, marker, proof)

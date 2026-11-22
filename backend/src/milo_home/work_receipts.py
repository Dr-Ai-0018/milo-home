"""Validate private terminal receipts against their original RPC evidence.

Only an already sealed output may accompany the fixed-field terminal facts;
validation never extracts raw protocol text for publication.
"""

import re
from uuid import UUID

from .codex_rpc import ProtocolError
from .work_decision_native import WORK_CHOICE_SCHEMA
from .work_decision_result import WorkDecisionResult
from .work_output_models import WorkOutputView

WORK_FIELDS = {"input_id", "permit_id", "window_id", "task_id", "stream_id"}
RECEIPT_FIELDS = {"kind", "work", "request_id", "turn_id", "outcome", "observed_at"}
OUTCOMES = {"completed", "failed", "interrupted"}


def native_failure_shape(proof):
    return (
        isinstance(proof, dict)
        and set(proof) == {"rollout_sha256", "error_sequence"}
        and isinstance(proof["rollout_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", proof["rollout_sha256"])
        and type(proof["error_sequence"]) is int
        and proof["error_sequence"] > 0
    )


def receipt_shape(event):
    try:
        valid = (
            set(event)
            in (
                RECEIPT_FIELDS,
                RECEIPT_FIELDS | {"output"},
                RECEIPT_FIELDS | {"native_failure"},
                RECEIPT_FIELDS | {"output", "decision"},
            )
            and event["kind"] == "work_terminal"
            and set(event["work"]) == WORK_FIELDS
            and event["outcome"] in OUTCOMES
            and type(event["request_id"]) is int
            and event["request_id"] > 0
            and type(event["observed_at"]) is int
            and event["observed_at"] >= 0
            and isinstance(event["turn_id"], str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event["turn_id"])
            and re.fullmatch(r"[0-9a-f]{32}", event["work"]["stream_id"])
        )
        for field in WORK_FIELDS - {"stream_id"}:
            UUID(event["work"][field])
        if "output" in event:
            WorkOutputView.model_validate(event["output"])
        if "decision" in event:
            decision = WorkDecisionResult.model_validate(event["decision"])
            decision.require_time(event["observed_at"])
            if event["outcome"] != "completed" or event["output"]["answer"] != {
                "state": "available",
                "body": decision.receipt,
                "reason": None,
            }:
                raise ValueError("Work choice differs from its sealed readable receipt")
        if "native_failure" in event and (
            event["outcome"] != "failed" or not native_failure_shape(event["native_failure"])
        ):
            raise ValueError()
        if not valid:
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProtocolError("Invalid private work receipt") from None


def validated_receipts(entries, *, stream_id, window_id, task_id, thread_id):
    intents = {}
    responses = {}
    terminals = {}
    receipts = []
    seen_inputs = set()
    seen_turns = set()
    prior_errors = {}
    for sequence, event in entries:
        kind = event.get("kind")
        if kind == "intent":
            request = event["request"]
            intents[request["id"]] = event
        elif kind == "response":
            responses[event["id"]] = event["response"]
        elif kind == "notification":
            message = event["message"]
            params = message.get("params", {})
            if message["method"] == "turn/completed" and params.get("threadId") == thread_id:
                turn = params.get("turn", {})
                terminals.setdefault(turn.get("id"), set()).add(turn.get("status"))
            elif message["method"] == "error" and params.get("threadId") == thread_id:
                prior_errors[sequence] = params
        elif kind == "work_terminal":
            receipt_shape(event)
            work = event["work"]
            original = intents.get(event["request_id"], {})
            request = original.get("request", {})
            params = request.get("params", {})
            response = responses.get(event["request_id"], {})
            turn = response.get("result", {}).get("turn", {})
            statuses = set(terminals.get(event["turn_id"], set()))
            if turn.get("status") in OUTCOMES:
                statuses.add(turn["status"])
            native = event.get("native_failure")
            if native:
                error = prior_errors.get(native["error_sequence"], {})
                if error.get("turnId") != event["turn_id"] or error.get("willRetry") is not False:
                    raise ProtocolError("Native failure differs from original protocol")
                statuses.add("failed")
            if (
                work["stream_id"] != stream_id
                or work["window_id"] != window_id
                or work["task_id"] != task_id
                or work != original.get("work")
                or request.get("method") != "turn/start"
                or params.get("threadId") != thread_id
                or params.get("clientUserMessageId") != work["input_id"]
                or response.get("id") != event["request_id"]
                or turn.get("id") != event["turn_id"]
                or statuses != {event["outcome"]}
                or work["input_id"] in seen_inputs
                or event["turn_id"] in seen_turns
                or params.get("outputSchema") not in (None, WORK_CHOICE_SCHEMA)
                or (
                    event["outcome"] == "completed"
                    and (params.get("outputSchema") == WORK_CHOICE_SCHEMA) != ("decision" in event)
                )
                or ("decision" in event and params.get("outputSchema") != WORK_CHOICE_SCHEMA)
            ):
                raise ProtocolError("Work receipt does not match private evidence")
            seen_inputs.add(work["input_id"])
            seen_turns.add(event["turn_id"])
            receipts.append((sequence, event))
    for _, receipt in receipts:
        if terminals.get(receipt["turn_id"], {receipt["outcome"]}) != {receipt["outcome"]}:
            raise ProtocolError("Conflicting private terminal evidence")
    return receipts

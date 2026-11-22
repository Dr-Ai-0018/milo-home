"""Decode one exact-turn native work result before ephemeral disclosure checks disappear."""

from .codex_rpc import ProtocolError
from .work_decision_result import WorkDecisionResult
from .work_output import _items, extract_output

WORK_CHOICE_SCHEMA = WorkDecisionResult.model_json_schema()


def decision_output(entries, *, thread_id, turn_id, observed_at, policy):
    items, conflict = _items(entries, thread_id, turn_id)
    finals = [
        item.get("text")
        for item in items
        if item["type"] == "agentMessage" and item.get("phase") == "final_answer"
    ]
    if conflict or len(finals) != 1 or not isinstance(finals[0], str):
        raise ProtocolError("Work choice requires exactly one unambiguous final answer")
    if len(finals[0].encode()) > 128 * 1024:
        raise ProtocolError("Work choice exceeds the result transport boundary")
    try:
        choice = WorkDecisionResult.model_validate_json(finals[0])
        choice.require_time(observed_at)
    except ValueError as error:
        raise ProtocolError("Native work choice is invalid") from error
    if policy is None or not policy.permits(choice.receipt):
        raise ProtocolError("Native work receipt cannot be safely published")
    output = extract_output(entries, thread_id=thread_id, turn_id=turn_id, policy=policy)
    output["answer"] = {"state": "available", "body": choice.receipt, "reason": None}
    return choice, output

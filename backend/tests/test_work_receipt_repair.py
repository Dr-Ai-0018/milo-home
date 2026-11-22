from copy import deepcopy
from uuid import uuid4

import pytest

from milo_home.codex_rpc import ProtocolError
from milo_home.work_receipt_repair import repair_candidates


@pytest.fixture
def evidence():
    scope = {
        "stream_id": uuid4().hex,
        "window_id": str(uuid4()),
        "task_id": str(uuid4()),
        "thread_id": "synthetic-main",
    }
    work = {k: v for k, v in scope.items() if k != "thread_id"}
    work.update(input_id=str(uuid4()), permit_id=str(uuid4()))
    entries = [
        (
            1,
            {
                "kind": "intent",
                "work": work,
                "request": {
                    "id": 3,
                    "method": "turn/start",
                    "params": {
                        "threadId": scope["thread_id"],
                        "clientUserMessageId": work["input_id"],
                        "input": [{"type": "text", "text": "private input"}],
                    },
                },
            },
        ),
        (
            2,
            {
                "kind": "response",
                "id": 3,
                "response": {"id": 3, "result": {"turn": {"id": "t1", "status": "inProgress"}}},
            },
        ),
        (
            3,
            {
                "kind": "notification",
                "message": {
                    "method": "turn/completed",
                    "params": {
                        "threadId": scope["thread_id"],
                        "turn": {"id": "t1", "status": "completed"},
                    },
                },
            },
        ),
    ]
    return entries, scope


def recover(evidence):
    entries, scope = evidence
    return repair_candidates(entries, **scope, observed_at=100)


@pytest.mark.parametrize("outcome", ["completed", "failed", "interrupted"])
def test_repairs_only_exact_facts_with_current_observation_time(evidence, outcome):
    entries, _ = evidence
    entries[-1][1]["message"]["params"]["turn"]["status"] = outcome
    before = deepcopy(entries)
    candidates, sources = recover(evidence)
    assert len(candidates) == 1 and candidates[0]["observed_at"] == 100
    assert candidates[0]["outcome"] == outcome and "output" not in candidates[0]
    assert entries == before
    assert sources == [
        {
            "input_id": candidates[0]["work"]["input_id"],
            "request_sequence": 1,
            "response_sequence": 2,
            "terminal_sequences": [3],
        }
    ]


def test_terminal_response_is_sufficient_without_notification(evidence):
    entries, _ = evidence
    entries.pop()
    entries[1][1]["response"]["result"]["turn"]["status"] = "completed"
    candidates, sources = recover(evidence)
    assert candidates[0]["outcome"] == "completed" and sources[0]["terminal_sequences"] == []


def test_early_notification_and_identical_duplicate_terminal_are_safe(evidence):
    entries, _ = evidence
    entries[1], entries[2] = (2, entries[2][1]), (3, entries[1][1])
    entries.append((4, deepcopy(entries[1][1])))
    candidates, sources = recover(evidence)
    assert len(candidates) == 1 and sources[0]["terminal_sequences"] == [2, 4]


@pytest.mark.parametrize("missing", ["response", "terminal", "foreign-thread", "foreign-turn"])
def test_incomplete_or_other_turn_evidence_does_not_create_receipt(evidence, missing):
    entries, _ = evidence
    if missing == "response":
        entries.pop(1)
    elif missing == "terminal":
        entries.pop()
    elif missing == "foreign-thread":
        entries[-1][1]["message"]["params"]["threadId"] = "other-thread"
    else:
        entries[-1][1]["message"]["params"]["turn"]["id"] = "other-turn"
    assert recover(evidence) == ([], [])


@pytest.mark.parametrize(
    "corruption",
    ["response-id", "thread", "input", "work", "error", "conflict", "order", "duplicate-response"],
)
def test_conflicting_or_malformed_local_proof_is_rejected(evidence, corruption):
    entries, _ = evidence
    if corruption == "response-id":
        entries[1][1]["response"]["id"] = 99
    elif corruption == "thread":
        entries[0][1]["request"]["params"]["threadId"] = "other-thread"
    elif corruption == "input":
        entries[0][1]["request"]["params"]["clientUserMessageId"] = str(uuid4())
    elif corruption == "work":
        entries[0][1]["work"]["private"] = "forbidden"
    elif corruption == "error":
        entries[1][1]["response"]["error"] = {"message": "private"}
    elif corruption == "conflict":
        entries[1][1]["response"]["result"]["turn"]["status"] = "failed"
    elif corruption == "order":
        entries[0], entries[1] = (1, entries[1][1]), (2, entries[0][1])
    else:
        entries.append((4, deepcopy(entries[1][1])))
    with pytest.raises(ProtocolError):
        recover(evidence)


def test_existing_sealed_output_is_never_changed_or_repaired_again(evidence):
    entries, _ = evidence
    receipt = recover(evidence)[0][0]
    receipt["output"] = {
        "answer": {"state": "available", "body": "original sealed answer", "reason": None},
        "actions": {"command_calls": 1, "file_change_calls": 0},
    }
    entries.append((4, receipt))
    before = deepcopy(entries)
    assert recover(evidence) == ([], []) and entries == before

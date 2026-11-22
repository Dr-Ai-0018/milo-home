from copy import deepcopy
from uuid import uuid4

import pytest
from test_codex_journal import bound as bound
from test_codex_journal import unit
from test_journal_capacity import proof

from milo_home.codex_journal import BusinessBinding
from milo_home.codex_rpc import ProtocolError
from milo_home.journal_capacity import MIB, STREAM_LIMIT


@pytest.fixture
def repair_case(bound):
    journal, *_ = bound
    linked = BusinessBinding(
        *[str(uuid4()) for _ in range(4)],
        str(journal.path),
        1,
        1,
        journal.fingerprint(),
    )
    journal.bind_business(linked)
    work = {key: str(uuid4()) for key in ("input_id", "permit_id", "window_id", "task_id")}
    work["stream_id"] = "b" * 32
    journal.open_work_stream(
        linked,
        unit=unit(),
        stream=work["stream_id"],
        window_id=work["window_id"],
        task_id=work["task_id"],
    )
    request_sequence = journal.record(
        work["stream_id"],
        {
            "kind": "intent",
            "work": work,
            "request": {
                "id": 1,
                "method": "turn/start",
                "params": {
                    "threadId": journal.binding().thread_id,
                    "clientUserMessageId": work["input_id"],
                },
            },
        },
    )
    response_sequence = journal.record(
        work["stream_id"],
        {
            "kind": "response",
            "id": 1,
            "response": {"id": 1, "result": {"turn": {"id": "turn", "status": "completed"}}},
        },
    )
    receipt = {
        "kind": "work_terminal",
        "work": work,
        "request_id": 1,
        "turn_id": "turn",
        "outcome": "completed",
        "observed_at": 100,
    }
    marker = {
        "kind": "work_receipt_repair",
        "source": "original_protocol",
        "checked_at": 100,
        "evidence": [
            {
                "input_id": work["input_id"],
                "request_sequence": request_sequence,
                "response_sequence": response_sequence,
                "terminal_sequences": [],
            }
        ],
    }
    return journal, journal.work_snapshot(work["stream_id"]), receipt, marker, proof()


@pytest.mark.parametrize("damage", ["input", "provenance", "source", "time", "unit", "snapshot"])
def test_repair_storage_rejects_unattributable_facts_atomically(repair_case, damage):
    journal, expected, receipt, marker, stop = deepcopy(repair_case)
    original = journal.work_snapshot(expected["stream"]["id"])
    if damage == "input":
        receipt["work"]["input_id"] = str(uuid4())
    elif damage == "provenance":
        marker["evidence"][0]["request_sequence"] += 100
    elif damage == "source":
        marker["source"] = "made_up"
    elif damage == "time":
        marker["checked_at"] += 1
    elif damage == "unit":
        stop["unit"] = unit(2)
        stop["group"] = "/system.slice/" + unit(2)
    else:
        expected["requests"][0]["state"] = "unknown"
    with pytest.raises(ProtocolError):
        journal.append_repair(expected, [receipt], marker, stop)
    assert journal.work_snapshot(expected["stream"]["id"]) == original


def test_repair_uses_cleanup_space_and_preserves_original_sequences(repair_case):
    journal, expected, receipt, marker, stop = repair_case
    stream = expected["stream"]["id"]
    with journal._connect() as connection:
        connection.execute("UPDATE streams SET bytes=? WHERE id=?", (STREAM_LIMIT - MIB, stream))
    expected = journal.work_snapshot(stream)
    sequence = journal.append_repair(expected, [receipt], marker, stop)
    saved = journal.work_snapshot(stream)
    assert saved["entries"][:2] == expected["entries"]
    assert saved["entries"][-1] == (sequence, stop)
    assert saved["requests"] == expected["requests"]
    assert saved["stream"]["bytes"] < STREAM_LIMIT - MIB // 4


def test_repair_batch_retention_failure_has_no_partial_marker_or_receipt(repair_case):
    journal, expected, receipt, marker, stop = repair_case
    stream = expected["stream"]["id"]
    with journal._connect() as connection:
        connection.execute(
            "UPDATE streams SET bytes=? WHERE id=?", (STREAM_LIMIT - MIB // 4, stream)
        )
    expected = journal.work_snapshot(stream)
    with pytest.raises(ProtocolError, match="retention"):
        journal.append_repair(expected, [receipt], marker, stop)
    assert journal.work_snapshot(stream) == expected

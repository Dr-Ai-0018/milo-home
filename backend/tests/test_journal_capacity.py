from uuid import uuid4

import pytest
from test_codex_journal import bound as bound
from test_codex_journal import intent, unit

from milo_home import journal_capacity as limits
from milo_home.codex_rpc import ProtocolError


def used(journal, stream, size):
    with journal._connect() as connection:
        connection.execute("UPDATE streams SET bytes=? WHERE id=?", (size, stream))


def proof():
    return {
        "kind": "work_stop",
        "unit": unit(),
        "group": "/system.slice/" + unit(),
        "boot_id": str(uuid4()),
        "invocation": "a" * 32,
        "main_pid": 42,
        "checked_at": 100,
    }


@pytest.mark.parametrize("global_limit", [False, True])
def test_ordinary_boundary_preserves_cleanup_reserve(bound, global_limit):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    limit = limits.TOTAL_LIMIT if global_limit else limits.STREAM_LIMIT
    if global_limit:
        with journal._connect() as connection:
            for number in range(2, 5):
                connection.execute(
                    "INSERT INTO streams VALUES (?,?,'closed',?)",
                    (f"{number:032x}", unit(number), limits.STREAM_LIMIT),
                )
    event = {"kind": "notification", "message": {"method": "synthetic"}}
    size = len(limits.encode(event).encode())
    # The other closed streams account for 24MiB in the global case.
    used(
        journal,
        stream,
        limit - limits.ORDINARY_RESERVE - size - (24 * limits.MIB if global_limit else 0),
    )
    journal.record(stream, event)
    before = journal.stream_snapshot(stream)
    with pytest.raises(ProtocolError, match="cleanup space reserved"):
        journal.record(stream, event)
    assert journal.stream_snapshot(stream) == before


def test_maximum_response_then_unknown_and_explicit_stop_have_room(bound):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    journal.record(stream, intent())
    used(journal, stream, limits.STREAM_LIMIT - limits.ORDINARY_RESERVE)
    response = {"kind": "response", "id": 1, "response": {"padding": ""}}
    response["response"]["padding"] = "x" * (
        limits.ENTRY_LIMIT - len(limits.encode(response).encode())
    )
    assert len(limits.encode(response).encode()) == limits.ENTRY_LIMIT
    journal.record(stream, response)
    journal.record(stream, {"kind": "unknown", "id": 1})
    with pytest.raises(ProtocolError, match="unavailable"):
        journal.record(stream, {"kind": "process", "unit": unit(), "phase": "stop_confirmed"})
    sequence = journal.record(stream, proof())
    assert journal.stream_snapshot(stream)["entries"][-1][0] == sequence
    assert journal.outstanding()[0]["delivery"] == "unknown"
    assert not journal.close_stream(stream, stop=lambda: True)


def test_oversized_response_leaves_pending_and_small_unknown_can_be_recorded(bound):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    journal.record(stream, intent())
    with pytest.raises(ProtocolError, match="entry limit"):
        journal.record(stream, {"kind": "response", "id": 1, "response": "x" * limits.ENTRY_LIMIT})
    assert journal.outstanding()[0]["delivery"] == "pending"
    journal.record(stream, {"kind": "unknown", "id": 1})
    assert journal.outstanding()[0]["delivery"] == "unknown"


def test_fake_cleanup_payload_cannot_consume_reserved_space(bound):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    journal.record(stream, intent())
    before = journal.stream_snapshot(stream)
    for event in (
        {"kind": "unknown", "id": 1, "padding": "fake"},
        {"kind": "process", "unit": unit(), "phase": "stop_confirmed", "padding": "x" * 4096},
    ):
        with pytest.raises(ProtocolError):
            journal.record(stream, event)
    assert journal.stream_snapshot(stream) == before


def test_hard_full_does_not_release_unknown_or_overwrite_evidence(bound):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    journal.record(stream, intent())
    used(journal, stream, limits.STREAM_LIMIT)
    before = journal.stream_snapshot(stream)
    with pytest.raises(ProtocolError, match="retention"):
        journal.record(stream, {"kind": "unknown", "id": 1})
    assert journal.stream_snapshot(stream) == before
    assert not journal.close_stream(stream, stop=lambda: True)


def test_new_stream_requires_full_stream_budget_without_changing_existing_records(bound):
    journal, *_ = bound
    with journal._connect() as connection:
        for number in range(1, 5):
            connection.execute(
                "INSERT INTO streams VALUES (?,?,'closed',?)",
                (f"{number:032x}", unit(number), 7 * limits.MIB),
            )
    before = journal.list_streams()
    assert journal.capacity()["can_open_stream"] is False
    with pytest.raises(ProtocolError, match="archival"):
        journal.open_stream(unit(5))
    assert journal.list_streams() == before


def test_stream_snapshot_preserves_original_sequences_and_optional_work(bound):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    sequence = journal.record(stream, intent())
    snapshot = journal.stream_snapshot(stream)
    assert snapshot["work"] is None
    assert snapshot["entries"] == [(sequence, intent())]
    assert journal.list_streams(unit=unit())[0]["stream"] == snapshot["stream"]
    assert journal.list_streams(unit=unit(2)) == []
    with pytest.raises(ProtocolError, match="work stream"):
        journal.work_snapshot(stream)

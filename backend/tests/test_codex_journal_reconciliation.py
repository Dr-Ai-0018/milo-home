import pytest
from test_codex_journal import bound as bound
from test_codex_journal import intent, unit

from milo_home.codex_rpc import ProtocolError


def proof(**changes):
    return {
        "kind": "work_stop",
        "unit": unit(),
        "group": "/system.slice/" + unit(),
        "boot_id": "12345678-1234-1234-1234-123456789abc",
        "invocation": "a" * 32,
        "main_pid": 42,
        "checked_at": 100,
        **changes,
    }


def test_closed_stream_only_accepts_new_stop_and_requires_latest_proof(bound):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    assert journal.close_stream(stream, stop=lambda: True)
    first = journal.record(stream, proof())
    latest = journal.record(stream, proof(checked_at=101))
    assert latest > first
    with pytest.raises(ProtocolError, match="Fresh"):
        journal.reconcile_stream(stream, first)
    with pytest.raises(ProtocolError, match="unavailable"):
        journal.record(stream, intent())
    journal.reconcile_stream(stream, latest)
    assert not journal.outstanding()


@pytest.mark.parametrize("state", ["pending", "unknown"])
def test_new_stop_never_acknowledges_an_unresolved_request(bound, state):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    journal.record(stream, intent())
    if state == "unknown":
        journal.record(stream, {"kind": "unknown", "id": 1})
    sequence = journal.record(stream, proof())
    with pytest.raises(ProtocolError, match="Unknown delivery"):
        journal.reconcile_stream(stream, sequence)
    assert journal.outstanding()[0]["delivery"] == state


@pytest.mark.parametrize(
    "changes", [{"unit": unit(2)}, {"checked_at": True}, {"group": "/../" + unit()}]
)
def test_stop_proof_rejects_wrong_scope_or_malformed_identity(bound, changes):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    with pytest.raises(ProtocolError):
        journal.record(stream, proof(**changes))
    with pytest.raises(ProtocolError, match="Fresh"):
        journal.reconcile_stream(stream, 1)

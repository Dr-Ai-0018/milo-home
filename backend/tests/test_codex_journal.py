import json
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from milo_home.codex_app_server import MainConversation, ThreadBinding
from milo_home.codex_config import CodexRuntime, NewawzConfig
from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import DeliveryUnknown, ProtocolError, StdioRpc


@pytest.fixture
def bound(tmp_path):
    config = NewawzConfig("synthetic-provider", "synthetic-model", "SYNTHETIC_KEY", b"synthetic")
    runtime = CodexRuntime(tmp_path / "codex", tmp_path / "codex-code-mode-host")
    binding = ThreadBinding("main-thread", config.provider, config.model, tmp_path)
    config.materialize(tmp_path)
    journal = PrivateJournal(tmp_path)
    journal.bind(binding, config, runtime)
    return journal, binding, config, runtime


def unit(index=1):
    return f"milo-work-{index:032x}.service"


def intent(request_id=1):
    return {"kind": "intent", "request": {"id": request_id, "method": "turn/start", "params": {}}}


def test_exact_binding_survives_restart_and_cannot_be_replaced(bound, tmp_path):
    journal, binding, config, runtime = bound
    reopened = PrivateJournal(tmp_path)
    assert reopened.binding() == binding
    reopened.bind(binding, config, runtime)
    other = ThreadBinding("another-thread", config.provider, config.model, tmp_path)
    with pytest.raises(ProtocolError, match="binding changed"):
        reopened.bind(other, config, runtime)
    changed = NewawzConfig(config.provider, config.model, config.env_key, b"changed endpoint")
    with pytest.raises(ProtocolError, match="registered configuration"):
        reopened.bind(binding, changed, runtime)
    assert journal.binding() == binding


def test_legacy_binding_remains_byte_identical_and_uses_its_original_paths(bound):
    journal, binding, config, runtime = bound
    with journal._connect() as connection:
        value = json.loads(connection.execute("SELECT value FROM binding").fetchone()[0])
        value["version"] = "codex-cli 0.162.1"
        connection.execute("UPDATE binding SET value=?", (json.dumps(value, sort_keys=True),))
    fingerprint = journal.fingerprint()
    opened = PrivateJournal(journal.path.parent, initialize=False, read_only=True)
    assert opened.installation() == runtime
    opened.bind(binding, config, runtime)
    assert opened.fingerprint() == fingerprint
    unrelated = CodexRuntime(runtime.cli.with_name("unrelated"), runtime.tool_host)
    with pytest.raises(ProtocolError, match="binding changed"):
        opened.bind(binding, config, unrelated)


def test_unbound_journal_cannot_open_or_guess_a_thread(tmp_path):
    journal = PrivateJournal(tmp_path)
    with pytest.raises(ProtocolError, match="No persistent"):
        journal.binding()
    with pytest.raises(ProtocolError, match="Bind"):
        journal.open_stream(unit())


def test_committed_intent_survives_abrupt_coordinator_exit(bound, tmp_path):
    program = (
        "import os,sys; from pathlib import Path; "
        "from milo_home.codex_journal import PrivateJournal; "
        "journal=PrivateJournal(Path(sys.argv[1])); "
        "stream=journal.open_stream(sys.argv[2]); "
        "journal.record(stream, {'kind':'intent', 'request': "
        "{'id':1, 'method':'turn/start', 'params':{}}}); os._exit(91)"
    )
    result = subprocess.run([sys.executable, "-c", program, str(tmp_path), unit()], timeout=5)
    assert result.returncode == 91
    reopened = PrivateJournal(tmp_path)
    assert reopened.outstanding()[0]["delivery"] == "pending"
    with pytest.raises(ProtocolError, match="reconciliation"):
        reopened.open_stream(unit(2))


@pytest.mark.parametrize("explicit_unknown", [False, True])
def test_lost_response_survives_restart_and_stopping_does_not_make_it_replayable(
    bound, tmp_path, explicit_unknown
):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    journal.record(stream, intent())
    if explicit_unknown:
        journal.record(stream, {"kind": "unknown", "id": 1})
    reopened = PrivateJournal(tmp_path)
    rows = reopened.outstanding()
    assert rows[0]["unit"] == unit()
    assert rows[0]["delivery"] == ("unknown" if explicit_unknown else "pending")
    assert not reopened.close_stream(stream, stop=lambda: True)
    with pytest.raises(ProtocolError, match="reconciliation"):
        reopened.open_stream(unit(2))
    with pytest.raises(ProtocolError):
        reopened.record(stream, intent(2))


def test_only_confirmed_stop_with_known_deliveries_closes_stream(bound, tmp_path):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    journal.record(stream, intent())
    journal.record(stream, {"kind": "response", "id": 1, "response": {"id": 1, "result": {}}})
    assert not journal.close_stream(stream, stop=lambda: False)
    assert PrivateJournal(tmp_path).outstanding()
    assert journal.close_stream(stream, stop=lambda: True)
    assert not PrivateJournal(tmp_path).outstanding()
    assert journal.open_stream(unit(2)) != stream


def test_second_actor_is_rejected_until_first_closes(bound, tmp_path):
    def attempt(index):
        try:
            return PrivateJournal(tmp_path).open_stream(unit(index))
        except ProtocolError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, [1, 2]))
    assert sum(value is not None for value in results) == 1


def test_invalid_record_rolls_back_and_duplicate_intent_does_not_replace_history(bound):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    journal.record(stream, intent())
    with pytest.raises(ProtocolError):
        journal.record(stream, {"kind": "response", "id": 2, "response": {}})
    assert journal.outstanding()[0]["delivery"] == "pending"
    with pytest.raises(ProtocolError):
        journal.record(stream, intent())
    with journal._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1


def test_unknown_after_durable_ack_still_blocks_following_stream(bound):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    journal.record(stream, intent())
    journal.record(stream, {"kind": "response", "id": 1, "response": {}})
    journal.record(stream, {"kind": "unknown", "id": 1})
    assert not journal.close_stream(stream, stop=lambda: True)
    assert journal.outstanding()[0]["delivery"] == "unknown"


def test_retention_limit_keeps_unanswered_intent_blocked(bound):
    journal, *_ = bound
    stream = journal.open_stream(unit())
    journal.record(stream, intent())
    with journal._connect() as connection:
        connection.execute("UPDATE streams SET bytes=?", (8 * 1024 * 1024,))
    with pytest.raises(ProtocolError, match="retention"):
        journal.record(stream, {"kind": "response", "id": 1, "response": {}})
    assert journal.outstanding()[0]["delivery"] == "pending"


def test_private_database_symlink_permissions_and_unknown_version_are_rejected(tmp_path):
    journal = PrivateJournal(tmp_path)
    journal.path.chmod(0o644)
    with pytest.raises(ProtocolError, match="private"):
        PrivateJournal(tmp_path)
    journal.path.chmod(0o600)
    with sqlite3.connect(journal.path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(ProtocolError, match="version"):
        PrivateJournal(tmp_path)
    other = tmp_path / "other.sqlite3"
    journal.path.rename(other)
    journal.path.symlink_to(other)
    with pytest.raises(ProtocolError, match="non-linked"):
        PrivateJournal(tmp_path)


@pytest.mark.parametrize("scenario", ["normal", "eof"])
def test_real_stdio_transport_uses_durable_journal_without_model_calls(bound, tmp_path, scenario):
    journal, binding, *_ = bound
    stream = journal.open_stream(unit())
    output = tmp_path / "received.ndjson"
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
    )

    def stop():
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=3)
        return True  # Synthetic peer never spawns tools or descendants.

    rpc = StdioRpc(
        process,
        deadline=time.monotonic() + 5,
        record=journal.recorder(stream),
        stop=stop,
        checkpoint=lambda: None,
    )
    session = MainConversation(rpc, binding, authorize_turn=lambda input_id: True)
    try:
        session.resume()
        if scenario == "eof":
            with pytest.raises(DeliveryUnknown):
                session.start("不能自动重跑", input_id="input-1")
        else:
            session.start("只发这次正文", input_id="input-1")
            assert session.wait() == "completed"
    finally:
        assert rpc.close()
    assert journal.close_stream(stream, stop=stop) is (scenario == "normal")
    restarted = PrivateJournal(tmp_path)
    assert restarted.binding() == binding
    requests = [json.loads(line) for line in output.read_text().splitlines()]
    assert sum(r["method"] == "turn/start" for r in requests) == 1
    if scenario == "eof":
        assert restarted.outstanding()[0]["delivery"] == "unknown"
    else:
        assert not restarted.outstanding()


@pytest.mark.parametrize(
    "changes",
    [
        {"unit": "other.service"},
        {"phase": "unrecognized"},
        {"invocation": "invalid"},
        {"main_pid": True},
        {"main_pid": 0},
    ],
)
def test_process_evidence_rejects_wrong_scope_and_missing_identity(bound, changes):
    journal, *_ = bound
    name = unit()
    stream = journal.open_stream(name)
    event = {
        "kind": "process",
        "unit": name,
        "phase": "ready",
        "invocation": "a" * 32,
        "main_pid": 42,
        **changes,
    }
    with pytest.raises(ProtocolError):
        journal.record(stream, event)
    with journal._connect() as connection:
        assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 0

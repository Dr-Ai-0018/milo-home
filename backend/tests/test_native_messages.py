import json
import os
import sqlite3
from uuid import uuid4

import pytest
from test_native_policy import configure
from test_native_policy import execution_case as execution_case
from test_native_policy import native_case as native_case
from test_native_policy import runtime_case as runtime_case
from test_native_policy import work_case as work_case

from milo_home import native_message_capacity
from milo_home.codex_rpc import ProtocolError
from milo_home.execution_lock import execution_lock
from milo_home.native_messages import NativeMessageEvent, NativeMessageScope, NativeMessageStore
from milo_home.native_reply_input import (
    native_input_digest,
    native_reply_input,
    native_reply_result,
)

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Private native routing requires root")


@pytest.fixture
def message_case(native_case, monkeypatch, capsys):
    policy = configure(native_case, monkeypatch, capsys)
    case, runtime, journal = native_case
    linked = journal.business_binding()
    scope = NativeMessageScope(
        run_id=uuid4(),
        owner_id=case[0][1],
        conversation_id=uuid4(),
        business_binding_id=linked.binding_id,
        baseline_fingerprint=journal.fingerprint(),
        policy_digest=policy["digest"],
        native_thread_id=journal.binding().thread_id,
        message_ids=(uuid4(), uuid4()),
        input_digest="a" * 64,
    )
    return runtime, NativeMessageStore(journal), scope


def event(kind, **fields):
    return NativeMessageEvent(kind=kind, stream_id="b" * 32, request_id=3, **fields)


def test_read_only_inspection_creates_nothing(message_case):
    runtime, store, scope = message_case
    before = set(store.directory.iterdir())
    assert store.inspect(scope.run_id) is None
    assert set(store.directory.iterdir()) == before


def test_routing_and_turn_survive_restart_without_another_delivery(message_case):
    runtime, store, scope = message_case
    with execution_lock(runtime.database) as lease:
        store.claim(scope, lease=lease)
        store.claim(scope, lease=lease)
        store.record(scope, event("sent"), lease=lease)
        store.record(scope, event("accepted", turn_id="native-turn"), lease=lease)
        store.record(
            scope,
            event("terminal", turn_id="native-turn", outcome="completed", result_digest="c" * 64),
            lease=lease,
        )
        store.record(
            scope, event("stopped", turn_id="native-turn", stop_confirmed=True), lease=lease
        )
    reopened = NativeMessageStore(runtime.journal)
    saved, events = reopened.inspect(scope.run_id)
    assert saved == scope
    assert [item.kind for item in events] == ["sent", "accepted", "terminal", "stopped"]
    assert events[1].turn_id == "native-turn"
    with execution_lock(runtime.database) as lease:
        with pytest.raises(ProtocolError, match="already attempted"):
            reopened.claim(scope, lease=lease)
        changed_run = scope.model_copy(update={"run_id": uuid4()})
        with pytest.raises(ProtocolError, match="earlier delivery"):
            reopened.claim(changed_run, lease=lease)
    assert reopened.inspect(changed_run.run_id) is None


def test_lost_ack_never_replays_even_after_confirmed_process_stop(message_case):
    runtime, store, scope = message_case
    with execution_lock(runtime.database) as lease:
        store.claim(scope, lease=lease)
        store.record(scope, event("sent"), lease=lease)
        store.record(scope, event("stopped", stop_confirmed=False), lease=lease)
        store.record(scope, event("stopped", stop_confirmed=True), lease=lease)
        with pytest.raises(ProtocolError, match="already attempted"):
            store.claim(scope, lease=lease)
        with pytest.raises(ProtocolError):
            store.record(scope, event("accepted", turn_id="late-turn"), lease=lease)
    _, events = store.inspect(scope.run_id)
    assert events[-1].stop_confirmed is True and all(e.kind != "accepted" for e in events)


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", uuid4()),
        ("native_thread_id", "other-thread"),
        ("business_binding_id", uuid4()),
        ("policy_digest", "f" * 64),
    ],
)
def test_wrong_identity_or_policy_cannot_claim(message_case, field, value):
    runtime, store, scope = message_case
    with execution_lock(runtime.database) as lease:
        with pytest.raises(ProtocolError):
            store.claim(scope.model_copy(update={field: value}), lease=lease)
    assert not store.path.exists() and not store.required.exists()


@pytest.mark.parametrize("damage", ["missing", "public", "linked", "marker-missing"])
def test_ledger_damage_cannot_reset_message_deduplication(message_case, damage):
    runtime, store, scope = message_case
    with execution_lock(runtime.database) as lease:
        store.claim(scope, lease=lease)
    if damage == "missing":
        store.path.unlink()
    elif damage == "public":
        store.path.chmod(0o644)
    elif damage == "linked":
        store.path.rename(store.path.with_suffix(".saved"))
        store.path.symlink_to(store.path.with_suffix(".saved"))
    else:
        store.required.unlink()
    with pytest.raises((OSError, ValueError, ProtocolError)):
        store.inspect(scope.run_id)
    with execution_lock(runtime.database) as lease:
        with pytest.raises((OSError, ValueError, ProtocolError)):
            store.claim(scope.model_copy(update={"run_id": uuid4()}), lease=lease)


def test_wrong_turn_or_request_cannot_settle_delivery(message_case):
    runtime, store, scope = message_case
    with execution_lock(runtime.database) as lease:
        store.claim(scope, lease=lease)
        with pytest.raises(ProtocolError):
            store.record(scope, event("accepted", turn_id="turn"), lease=lease)
        store.record(scope, event("sent"), lease=lease)
        store.record(scope, event("accepted", turn_id="turn"), lease=lease)
        with pytest.raises(ProtocolError):
            store.record(
                scope, event("terminal", turn_id="other", outcome="completed"), lease=lease
            )
        with pytest.raises(ProtocolError):
            store.record(
                scope,
                event("terminal", turn_id="turn", outcome="completed").model_copy(
                    update={"request_id": 4}
                ),
                lease=lease,
            )
    assert [e.kind for e in store.inspect(scope.run_id)[1]] == ["sent", "accepted"]


def test_one_native_turn_cannot_be_attached_to_two_batches(message_case):
    runtime, store, scope = message_case
    other = scope.model_copy(update={"run_id": uuid4(), "message_ids": (uuid4(),)})
    with execution_lock(runtime.database) as lease:
        for batch in (scope, other):
            store.claim(batch, lease=lease)
            store.record(batch, event("sent"), lease=lease)
        store.record(scope, event("accepted", turn_id="unique-native-turn"), lease=lease)
        with pytest.raises(ProtocolError, match="another message delivery"):
            store.record(other, event("accepted", turn_id="unique-native-turn"), lease=lease)
    assert [e.kind for e in store.inspect(other.run_id)[1]] == ["sent"]


def test_incremental_input_omits_history_and_reply_result_cannot_change_routing():
    message_id, owner = str(uuid4()), str(uuid4())
    snapshot = {
        "run_id": str(uuid4()),
        "conversation_id": str(uuid4()),
        "conversation": {"kind": "chat", "title": None},
        "correspondent": {"display_name": "Synthetic owner"},
        "created_at": 100,
        "deadline_at": 200,
        "history": [{"body": "HISTORY_MUST_NOT_BE_RESENT"}],
        "history_window": {"earlier_omitted": True},
        "selected": [
            {
                "id": message_id,
                "role": "visitor",
                "body": "本轮新增。",
                "sequence": 9,
                "created_at": 99,
                "in_reply_to": None,
            }
        ],
    }
    text = native_reply_input(snapshot, owner)
    assert "HISTORY_MUST_NOT_BE_RESENT" not in text and "history_window" not in text
    assert message_id in text and owner in text and "本轮新增。" in text
    assert native_input_digest(text) == native_input_digest(native_reply_input(snapshot, owner))
    answer = json.dumps({"replies": [{"message_id": message_id, "body": "收到啦。"}]})
    assert native_reply_result(answer, [message_id]).replies[0].body == "收到啦。"
    with pytest.raises(ValueError):
        native_reply_result(answer, [str(uuid4())])


@pytest.mark.parametrize("page_size", [1024, 8192])
def test_near_page_limit_refuses_claim_but_keeps_terminal_and_stop_space(
    message_case, monkeypatch, page_size
):
    runtime, store, scope = message_case
    monkeypatch.setattr(native_message_capacity, "PAGE_LIMIT", 256)
    with execution_lock(runtime.database) as lease:
        store.claim(scope, lease=lease)
        store.record(scope, event("sent"), lease=lease)
    with sqlite3.connect(store.path) as connection:
        connection.execute(f"PRAGMA page_size={page_size}")
        connection.execute("VACUUM")
        connection.execute("CREATE TABLE synthetic_padding(body BLOB)")
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        connection.execute(
            "INSERT INTO synthetic_padding VALUES (?)", (b"x" * ((256 - pages - 80) * page_size),)
        )
    capacity = store.capacity()
    assert capacity["page_size"] == page_size and not capacity["has_capacity"]
    assert capacity["available_bytes"] < capacity["cleanup_reserve_bytes"]
    other = scope.model_copy(update={"run_id": uuid4(), "message_ids": (uuid4(),)})
    with execution_lock(runtime.database) as lease:
        with pytest.raises(ProtocolError, match="cleanup space reserved"):
            store.claim(other, lease=lease)
        assert store.inspect(other.run_id) is None
        store.record(scope, event("accepted", turn_id="turn"), lease=lease)
        store.record(scope, event("terminal", turn_id="turn", outcome="failed"), lease=lease)
        store.record(scope, event("stopped", turn_id="turn", stop_confirmed=False), lease=lease)
        store.record(scope, event("stopped", turn_id="turn", stop_confirmed=True), lease=lease)
    assert store.inspect(scope.run_id)[1][-1].stop_confirmed is True
    with sqlite3.connect(store.path) as connection:
        connection.execute("DELETE FROM synthetic_padding")
    capacity = store.capacity()
    assert capacity["reusable_pages"] > 0 and capacity["has_capacity"]


def test_read_only_capacity_creates_no_ledger_or_marker(message_case):
    _, store, _ = message_case
    before = set(store.directory.iterdir())
    assert store.capacity()["has_capacity"]
    assert set(store.directory.iterdir()) == before


def test_new_claim_counts_its_own_pages_separately_from_cleanup(message_case, monkeypatch):
    runtime, store, scope = message_case
    with execution_lock(runtime.database) as lease:
        store.claim(scope, lease=lease)
    with sqlite3.connect(store.path) as connection:
        used_pages = connection.execute("PRAGMA page_count").fetchone()[0]
    # Existing claim needs 128 pages, while another claim needs its own
    # 128 cleanup pages plus 64 pages for constructing the new routing rows.
    monkeypatch.setattr(native_message_capacity, "PAGE_LIMIT", used_pages + 256)
    assert store.capacity(additional=0)["has_capacity"]
    assert not store.capacity(additional=1)["has_capacity"]
    other = scope.model_copy(update={"run_id": uuid4(), "message_ids": (uuid4(),)})
    with execution_lock(runtime.database) as lease:
        with pytest.raises(ProtocolError, match="cleanup space reserved"):
            store.claim(other, lease=lease)
    assert store.inspect(other.run_id) is None


def test_oversized_cleanup_event_is_rejected_before_persistence(message_case):
    runtime, store, scope = message_case
    with execution_lock(runtime.database) as lease:
        store.claim(scope, lease=lease)
        store.record(scope, event("sent"), lease=lease)
        oversized = event("accepted", turn_id="turn").model_copy(update={"turn_id": "x" * 4096})
        with pytest.raises(ProtocolError, match="size limit"):
            store.record(scope, oversized, lease=lease)
    assert [item.kind for item in store.inspect(scope.run_id)[1]] == ["sent"]

"""Real SQLite and HTTP projections of mixed natural reply choices."""

import json
import sqlite3
from uuid import uuid4

import pytest
from test_runs import pending

from milo_home.conversation_models import ConversationMessagePage
from milo_home.conversations import ConversationService
from milo_home.database import Database
from milo_home.errors import ApiError
from milo_home.reply_choice_result import ReplyChoiceResult
from milo_home.reply_projection_evidence import read_projection, require_publication
from milo_home.reply_results import parse_result
from milo_home.wakeup_events import EventPlanner


def mixed_case(client):
    thread, store = pending(client, 5)
    now = int(store.clock())
    store.clock = lambda: now
    run = store.queue(thread, 3600)
    snapshot = store.snapshot(run)
    ids = [item["id"] for item in snapshot["selected"]]
    result = ReplyChoiceResult.model_validate(
        {
            "version": 2,
            "decisions": [
                {"kind": "reply", "message_ids": ids[:2], "body": "这两张合着回。"},
                {"kind": "no_reply", "message_ids": ids[2:3]},
                {"kind": "defer", "message_ids": ids[3:4], "revisit_at": now + 3600},
                {"kind": "reply", "message_ids": ids[4:], "body": "先说这一句，约好再补充。"},
            ],
            "revisits": [{"message_ids": ids[4:], "revisit_at": now + 7200}],
            "reception": None,
        }
    )
    store.started(run)
    return thread, store, run, snapshot["conversation_id"], ids, result, now


def check_projection(store, run, result):
    with store.database.snapshot() as connection:
        saved = dict(connection.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone())
        published = [
            dict(row) for row in connection.execute("SELECT * FROM messages WHERE run_id=?", (run,))
        ]
        ids = [
            row[0]
            for row in connection.execute(
                "SELECT message_id FROM run_messages WHERE run_id=?", (run,)
            )
        ]
        require_publication(
            result, saved, published, read_projection(connection, run), ids, saved["ended_at"]
        )


def test_mixed_decisions_are_atomic_findable_and_not_selected_again(client, tmp_path):
    thread, store, run, conversation, ids, result, now = mixed_case(client)
    assert store.publish(run, result)
    assert not store.publish(run, result)
    check_projection(store, run, result)
    endpoint = f"/api/conversations/{conversation}/messages"
    messages = client.get(endpoint).json()["messages"]
    replies = [item for item in messages if item["role"] == "milo"]
    assert len(replies) == 2 and len(messages) == 7
    assert set(replies[0]["reply_to_ids"]) == set(ids[:2])
    assert messages[0]["reply_decision"]["reply_id"] == messages[1]["reply_decision"]["reply_id"]
    assert messages[2]["reply_decision"]["state"] == "no_reply"
    assert messages[3]["reply_decision"]["revisit"]["due_at"] == now + 3600
    activity = client.get(f"/api/conversations/{conversation}/activity").json()
    assert activity["reply_summary"] == {
        "unprocessed_count": 0,
        "replied_count": 3,
        "no_reply_count": 1,
        "deferred_count": 1,
        "unknown_count": 0,
    }
    assert activity["reply_queue"]["pending_count"] == 1
    assert activity["reply_queue"]["available_count"] == 0
    assert activity["revisits"]["pending_count"] == 2
    assert client.get("/api/threads").json()[0]["pending_count"] == 1
    assert (
        client.get(f"/api/threads/{thread}/conversations").json()[0]["reply_summary"]
        == activity["reply_summary"]
    )
    assert EventPlanner(store.database, store.clock).plan()["discovered_messages"] == 0
    with pytest.raises(ApiError, match="nothing_pending"):
        store.queue(thread, 3600)
    backup = tmp_path / "choice-copy.sqlite3"
    store.database.backup(backup)
    copied = Database(backup)
    copied.initialize()
    with copied.snapshot() as connection:
        owner = connection.execute("SELECT owner_id FROM threads WHERE id=?", (thread,)).fetchone()[
            0
        ]
    restored = ConversationService(copied).list_messages(owner, conversation, None, 100)
    assert (
        ConversationMessagePage.model_validate(restored).model_dump(mode="json")["messages"]
        == messages
    )
    added = client.post(
        endpoint, json={"body": "后来新增的一张。"}, headers={"Idempotency-Key": str(uuid4())}
    )
    next_run = store.queue(thread, 3600)
    assert [item["id"] for item in store.snapshot(next_run)["selected"]] == [added.json()["id"]]


def test_partial_projection_failure_rolls_back_bodies_decisions_and_revisits(client):
    _, store, run, _, _, result, _ = mixed_case(client)
    with store.database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_choice BEFORE INSERT ON message_decisions "
            "WHEN NEW.state='deferred' BEGIN SELECT RAISE(ABORT,'synthetic outage'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic outage"):
        store.publish(run, result)
    with store.database.snapshot() as connection:
        for table in (
            "message_reply_links",
            "message_decisions",
            "revisit_sources",
            "revisit_intents",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM messages WHERE role='milo'").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0]
            == "running"
        )
    with store.database.connect(write=True) as connection:
        connection.execute("DROP TRIGGER fail_choice")
    assert store.publish(run, result)
    check_projection(store, run, result)


def test_all_no_reply_success_has_no_fake_body_and_can_be_verified(client):
    thread, store = pending(client, 2)
    run = store.queue(thread, 3600)
    ids = [item["id"] for item in store.snapshot(run)["selected"]]
    result = ReplyChoiceResult(
        version=2, decisions=[{"kind": "no_reply", "message_ids": ids}], revisits=[], reception=None
    )
    store.started(run)
    assert store.publish(run, result)
    assert store.get(run)["status"] == "succeeded"
    assert client.get("/api/threads").json()[0]["pending_count"] == 0
    check_projection(store, run, result)
    with store.database.connect(write=True) as connection:
        connection.execute("DELETE FROM message_decisions WHERE message_id=?", (ids[0],))
    with pytest.raises(ValueError, match="decisions differ"):
        check_projection(store, run, result)


def test_cross_conversation_input_is_rejected_before_any_projection(client):
    thread, store, run, _, ids, _, _ = mixed_case(client)
    owner = client.get("/api/auth/me").json()["id"]
    other = ConversationService(store.database).chat(owner, thread)
    response = client.post(
        f"/api/conversations/{other['id']}/messages",
        json={"body": "另一话题。"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    result = ReplyChoiceResult(
        version=2,
        decisions=[{"kind": "no_reply", "message_ids": ids + [response.json()["id"]]}],
        revisits=[],
        reception=None,
    )
    with pytest.raises(ValueError, match="frozen scope"):
        store.publish(run, result)
    with store.database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM message_decisions").fetchone()[0] == 0


def test_choice_and_old_sealed_formats_remain_distinct():
    identity = str(uuid4())
    old = {"replies": [{"message_id": identity, "body": "原格式。"}]}
    parsed = parse_result(json.dumps(old))
    assert parsed.replies[0].body == "原格式。"
    with pytest.raises(ValueError, match="requested format"):
        parse_result(json.dumps(old), version=2)

import sqlite3
from uuid import uuid4

import pytest
from conftest import own_thread
from test_dispatch import make_case
from test_runs import pending, result

from milo_home.errors import ApiError
from milo_home.reply_result import ReplyResult
from milo_home.run_queue import queue_run
from milo_home.wakeup_events import EventPlanner


def setup_conversations(client):
    thread, store = pending(client, 1)
    with store.database.connect() as connection:
        owner = connection.execute(
            "SELECT owner_id FROM threads WHERE id = ?", (thread,)
        ).fetchone()[0]
    conversations = client.app.state.conversations
    chat = conversations.chat(owner, thread)["id"]
    return thread, store, conversations, owner, chat


def test_manual_runs_keep_selection_history_and_replies_in_one_conversation(client):
    thread, store, service, owner, chat = setup_conversations(client)
    chat_message = service.post(owner, chat, "另一会话内容", str(uuid4()))[0]
    client.app.state.messages.post(owner, thread, "同一封信续轮", str(uuid4()))
    run = store.queue(thread, 60)
    snapshot = store.snapshot(run)
    assert len(snapshot["selected"]) == 2
    assert chat_message["id"] not in {row["id"] for row in snapshot["selected"]}
    assert snapshot["conversation_id"] == thread
    store.started(run)
    assert store.publish(run, result(store, run))
    chat_run = store.queue(thread, 60)
    chat_snapshot = store.snapshot(chat_run)
    assert chat_snapshot["history"] == []
    assert chat_snapshot["selected"][0]["id"] == chat_message["id"]
    assert chat_snapshot["conversation_id"] == chat
    store.started(chat_run)
    assert store.publish(chat_run, result(store, chat_run))
    chat_history = service.list_messages(owner, chat, 0, 100)["messages"]
    assert len(chat_history) == 2
    assert chat_history[1]["in_reply_to"] == chat_message["id"]
    assert chat_history[1]["conversation_id"] == chat
    assert client.get("/api/threads").json()[0]["pending_count"] == 0


def test_explicit_mixed_selection_fails_without_creating_occupancy(client):
    thread, store, service, owner, chat = setup_conversations(client)
    chat_message = service.post(owner, chat, "合成聊天", str(uuid4()))[0]
    with store.database.connect() as connection:
        ids = [row[0] for row in connection.execute("SELECT id FROM messages ORDER BY sequence")]
    with pytest.raises(ApiError, match="scope_changed"):
        with store.database.connect(write=True) as connection:
            queue_run(connection, thread, 60, int(store.clock()), ids)
    assert store.active() is None
    with store.database.connect(write=True) as connection:
        run = queue_run(connection, thread, 60, int(store.clock()), [chat_message["id"]])
    store.started(run)
    bad = ReplyResult.model_validate(
        {
            "replies": [
                {
                    "message_id": ids[0],
                    "body": "错误会话的合成回信",
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="scope"):
        store.publish(run, bad)


def test_database_guards_scope_and_legacy_insert_defaulting(client):
    thread, store, service, owner, chat = setup_conversations(client)
    chat_message = service.post(owner, chat, "合成聊天", str(uuid4()))[0]
    run = store.queue(thread, 60)
    with store.database.connect(write=True) as connection:
        for sql, params in [
            ("UPDATE messages SET conversation_id = NULL WHERE id = ?", (chat_message["id"],)),
            ("UPDATE runs SET conversation_id = ? WHERE id = ?", (chat, run)),
            ("INSERT INTO run_messages VALUES (?, ?, ?)", (run, chat_message["id"], thread)),
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, params)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO messages (id, thread_id, sequence, role, body, in_reply_to, run_id, "
                "created_at, conversation_id) VALUES (?, ?, 50, 'milo', '错误', ?, ?, 1, ?)",
                (str(uuid4()), thread, chat_message["id"], run, chat),
            )
        connection.execute(
            "INSERT INTO messages (id, thread_id, sequence, role, body, author_id, client_key, "
            "created_at) VALUES (?, ?, 60, 'visitor', '兼容旧内部写入', ?, ?, 1)",
            (str(uuid4()), thread, owner, str(uuid4())),
        )
        assert (
            connection.execute(
                "SELECT conversation_id FROM messages WHERE sequence = 60"
            ).fetchone()[0]
            == thread
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_automatic_reservations_isolate_conversations_and_keep_other_events_pending(client):
    thread, runs, dispatch, now = make_case(client)
    assert own_thread(client) == thread
    with runs.database.connect() as connection:
        owner = connection.execute(
            "SELECT owner_id FROM threads WHERE id = ?", (thread,)
        ).fetchone()[0]
    chat = client.app.state.conversations.chat(owner, thread)["id"]
    chat_message = client.app.state.conversations.post(owner, chat, "聊天合成文本", str(uuid4()))[0]
    planner = EventPlanner(runs.database, lambda: now[0])
    planner.plan()
    now[0] += 15
    ticket = dispatch.reserve(60)
    assert runs.get(ticket.run_id)["conversation_id"] == thread
    assert len(runs.snapshot(ticket.run_id)["selected"]) == 2
    assert dispatch.authorize_launch(ticket)
    runs.started(ticket.run_id)
    assert runs.publish(ticket.run_id, result(runs, ticket.run_id))
    dispatch.reconcile()
    second = dispatch.reserve(60)
    assert second is not None
    assert runs.get(second.run_id)["conversation_id"] == chat
    assert runs.snapshot(second.run_id)["selected"][0]["id"] == chat_message["id"]
    assert runs.snapshot(second.run_id)["history"] == []


def test_interleaved_messages_fill_one_conversation_batch_without_consuming_others(client):
    thread, runs, dispatch, now = make_case(client, count=1)
    owner = client.get("/api/auth/me").json()["id"]
    service = client.app.state.conversations
    chat = service.chat(owner, thread)["id"]
    for index in range(10):
        service.post(owner, chat, f"聊天 {index}", str(uuid4()))
        client.app.state.messages.post(owner, thread, f"书信 {index}", str(uuid4()))
    planner = EventPlanner(runs.database, lambda: now[0])
    planner.plan()
    now[0] += 15
    ticket = dispatch.reserve(60)
    snapshot = runs.snapshot(ticket.run_id)
    assert snapshot["conversation_id"] == thread
    assert len(snapshot["selected"]) == 10
    assert snapshot["selected"][-1]["sequence"] == 19
    with runs.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM wake_event_delivery").fetchone()[0] == 10
        assert (
            connection.execute(
                "SELECT count(*) FROM wake_event_delivery "
                "JOIN wake_events ON event_id = wake_events.id "
                "JOIN messages ON message_id = messages.id WHERE messages.conversation_id = ?",
                (chat,),
            ).fetchone()[0]
            == 0
        )

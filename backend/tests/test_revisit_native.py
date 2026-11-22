"""An actual native consumer revisits the original topic with no new visitor input."""

import json

import pytest
from test_native_reply_execution import native_reply_case as native_reply_case
from test_native_reply_execution import requests, tick
from test_reply_execution import latest, ready, send
from test_reply_readiness import preparation as preparation
from test_work_process import fake_units as fake_units

from milo_home.execution_lock import execution_lock
from milo_home.journal_retention_scope import inspect_stream
from milo_home.native_messages import NativeMessageStore
from milo_home.work_runtime import WorkRuntime


def deferred(client, native_reply_case, count=2):
    case, journal, _, scenario, _ = native_reply_case
    database, owner, *_ = case[0]
    case[2].configure("reply", owner, "milo-home-reply.timer", enabled=True, revisits=True)
    scenario[0] = "auto-reply-defer"
    send(client, case, count)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    original = latest(database)
    with database.snapshot() as connection:
        intent = dict(connection.execute("SELECT * FROM revisit_intents").fetchone())
    return case, database, owner, journal, scenario, intent, original


def report(case, journal, run):
    runtime = WorkRuntime(case[0][0], journal, lambda: case[1][0])
    stream = journal.list_streams(unit=run["unit_name"] + ".service")[0]["stream"]["id"]
    with execution_lock(runtime.database) as lease:
        return inspect_stream(runtime, stream, lease=lease, reply_directory=case[0][4])


@pytest.mark.parametrize(
    "scenario_name,decision",
    [
        ("auto-reply-followup", "replied"),
        ("auto-reply-quiet-revisit", "no_reply"),
        ("auto-reply-defer", "deferred"),
    ],
)
def test_due_revisit_without_new_messages_resumes_same_native_once(
    client, native_reply_case, scenario_name, decision
):
    case, database, _, journal, scenario, intent, original = deferred(client, native_reply_case)
    first_scope = NativeMessageStore(journal).inspect(original["id"])[0]
    before = len(requests(native_reply_case))
    assert tick(native_reply_case)["outcome"] == "waiting_or_other_entry_first"
    assert len(requests(native_reply_case)) == before
    case[1][0] = intent["due_at"]
    scenario[0] = scenario_name
    assert tick(native_reply_case)["outcome"] == "succeeded"
    run = latest(database)
    scope = NativeMessageStore(journal).inspect(run["id"])[0]
    assert scope.message_ids == ()
    assert set(scope.revisit.source_ids) == set(first_scope.message_ids)
    assert scope.native_thread_id == first_scope.native_thread_id
    with database.snapshot() as connection:
        assert (
            connection.execute(
                "SELECT state FROM revisit_intents WHERE id=?", (intent["id"],)
            ).fetchone()[0]
            == "completed"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM run_messages WHERE run_id=?", (run["id"],)
            ).fetchone()[0]
            == 0
        )
        assert {row[0] for row in connection.execute("SELECT state FROM message_reply_state")} == {
            decision
        }
        assert connection.execute("SELECT count(*) FROM revisit_claims").fetchone()[0] == 1
    check = report(case, journal, run)
    assert check["eligible"], check
    first = report(case, journal, original)
    assert first["eligible"], first
    before = len(requests(native_reply_case))
    assert tick(native_reply_case)["outcome"] != "succeeded"
    assert len(requests(native_reply_case)) == before
    starts = [item for item in requests(native_reply_case) if item["method"] == "turn/start"]
    assert len(starts) == 2
    payload = json.loads(starts[-1]["params"]["input"][0]["text"].rsplit("\n", 1)[-1])
    assert payload["source"] == "saved_owner_conversation_revisit" and payload["selected"] == []


@pytest.mark.parametrize("quiet", [False, True])
def test_replied_conversation_can_revisit_with_zero_pending_inputs_and_keep_old_history(
    client, native_reply_case, quiet
):
    case, journal, _, scenario, _ = native_reply_case
    database, owner, *_ = case[0]
    case[2].configure("reply", owner, "milo-home-reply.timer", enabled=True, revisits=True)
    scenario[0] = "auto-reply-future-supplement"
    send(client, case, 2)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    original = latest(database)
    before = report(case, journal, original)
    assert before["eligible"]
    assert client.get("/api/threads").json()[0]["pending_count"] == 0
    with database.snapshot() as connection:
        due = connection.execute("SELECT due_at FROM revisit_intents").fetchone()[0]
    case[1][0] = due
    scenario[0] = "auto-reply-quiet-revisit" if quiet else "auto-reply-followup"
    assert tick(native_reply_case)["outcome"] == "succeeded"
    run = latest(database)
    after = report(case, journal, original)
    assert after["eligible"] and after["business_digest"] == before["business_digest"]
    check = report(case, journal, run)
    assert check["eligible"], check
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM messages WHERE role='milo'").fetchone()[
            0
        ] == (1 if quiet else 2)
        assert {row[0] for row in connection.execute("SELECT state FROM message_reply_state")} == {
            "replied"
        }

"""Actual consumer, synthetic native transport, real sealing and HTTP readback."""

import pytest
from test_journal_retention_scope import reply_report
from test_native_reply_execution import native_reply_case as native_reply_case
from test_native_reply_execution import requests, tick
from test_reply_execution import latest, ready, send
from test_reply_readiness import preparation as preparation
from test_work_process import fake_units as fake_units

from milo_home.errors import ApiError
from milo_home.reply_receipts import publish_receipt
from milo_home.reply_recovery import replay
from milo_home.reply_result import OUTPUT_SCHEMA
from milo_home.reply_results import LEGACY_CHOICE_OUTPUT_SCHEMA


@pytest.mark.parametrize(
    "scenario,count,bodies,pending",
    [
        ("auto-reply-merged", 3, 1, 0),
        ("auto-reply-no-reply", 3, 0, 0),
        ("auto-reply-defer", 2, 0, 2),
        ("auto-reply-mixed", 4, 1, 1),
    ],
)
def test_native_choice_consumer_seals_publishes_and_does_not_repeat(
    client, native_reply_case, scenario, count, bodies, pending
):
    case, _, _, choice, _ = native_reply_case
    choice[0] = scenario
    send(client, case, count)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    database, _, thread, *_ = case[0]
    with database.snapshot() as connection:
        assert (
            connection.execute("SELECT count(*) FROM messages WHERE role='milo'").fetchone()[0]
            == bodies
        )
        assert connection.execute("SELECT count(*) FROM message_decisions").fetchone()[0] == count
        assert (
            connection.execute(
                "SELECT count(*) FROM legacy_launches WHERE authorized_at IS NOT NULL"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT count(*) FROM work_tasks").fetchone()[0] == 0
        sealed = connection.execute("SELECT result FROM reply_execution_results").fetchone()[0]
        assert '"version":2' in sealed
    assert client.get("/api/threads").json()[0]["pending_count"] == pending
    report = reply_report(native_reply_case)
    assert report["eligible"], report
    assert report["scope"]["published_messages"] == bodies
    before = len(requests(native_reply_case))
    assert tick(native_reply_case)["outcome"] == "waiting_or_other_entry_first"
    assert len(requests(native_reply_case)) == before
    if scenario == "auto-reply-no-reply":
        with database.connect(write=True) as connection:
            connection.execute("DELETE FROM message_decisions")
        assert reply_report(native_reply_case)["blocked_by"] == [
            "reply_message_projection_conflict"
        ]


def test_late_publication_uses_sealed_decision_time_and_never_reexecutes(
    client, native_reply_case, monkeypatch
):
    case, _, _, scenario, _ = native_reply_case
    scenario[0] = "auto-reply-mixed"
    database, owner, _, _, directory, *_ = case[0]
    send(client, case, 4)
    ready(case)
    with database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_decisions BEFORE INSERT ON message_decisions "
            "BEGIN SELECT RAISE(ABORT,'synthetic publication outage'); END"
        )
    with pytest.raises(ApiError, match="publication_pending"):
        tick(native_reply_case)
    run = latest(database)
    count = len(requests(native_reply_case))
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM revisit_intents").fetchone()[0] == 0
    with database.connect(write=True) as connection:
        connection.execute("DROP TRIGGER fail_decisions")
    case[1][0] += 7200
    monkeypatch.setattr("milo_home.reply_recovery.unit_empty", lambda _: True)
    monkeypatch.setattr("milo_home.native_reply_recovery.stop_family", lambda _: True)
    monkeypatch.setattr(
        "milo_home.reply_recovery.publish_receipt",
        lambda db, who, run_id: publish_receipt(db, who, run_id, clock=lambda: case[1][0]),
    )
    assert replay(database, directory, owner, run["id"])["outcome"] == "succeeded"
    assert replay(database, directory, owner, run["id"])["outcome"] == "succeeded"
    assert len(requests(native_reply_case)) == count
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM message_decisions").fetchone()[0] == 4
        assert (
            connection.execute("SELECT count(*) FROM messages WHERE role='milo'").fetchone()[0] == 1
        )


@pytest.mark.parametrize(
    "schema", [OUTPUT_SCHEMA, LEGACY_CHOICE_OUTPUT_SCHEMA], ids=["v1", "v2-original-schema"]
)
def test_old_native_output_contract_remains_readable_and_archivable(
    client, native_reply_case, monkeypatch, schema
):
    case = native_reply_case[0]
    monkeypatch.setattr("milo_home.native_reply_delivery.CHOICE_OUTPUT_SCHEMA", schema)
    send(client, case)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    assert reply_report(native_reply_case)["eligible"]
    with case[0][0].snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM message_decisions").fetchone()[0] == (
            0 if schema == OUTPUT_SCHEMA else 1
        )
        assert (
            connection.execute("SELECT count(*) FROM messages WHERE role='milo'").fetchone()[0] == 1
        )


def test_revisit_later_state_does_not_rewrite_original_native_birth_evidence(
    client, native_reply_case
):
    case = native_reply_case[0]
    native_reply_case[3][0] = "auto-reply-defer"
    send(client, case)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    before = reply_report(native_reply_case)
    with case[0][0].connect(write=True) as connection:
        connection.execute(
            "UPDATE revisit_intents SET state='blocked',blocked_reason='reception',"
            "revision=revision+1,updated_at=updated_at+1"
        )
    after = reply_report(native_reply_case)
    assert before["eligible"] and after["eligible"]
    assert before["business_digest"] == after["business_digest"]

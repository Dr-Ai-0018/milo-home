"""New arrivals, cancelled in-flight work, unknown stops and sealed-only revisit recovery."""

import pytest
from test_native_reply_execution import native_reply_case as native_reply_case
from test_native_reply_execution import requests, tick
from test_reply_execution import latest, ready, send
from test_reply_readiness import preparation as preparation
from test_revisit_native import deferred, report
from test_work_process import fake_units as fake_units

from milo_home.errors import ApiError
from milo_home.execution_control import execution_busy
from milo_home.native_reply_delivery import NativeReplyDelivery
from milo_home.reply_receipts import publish_receipt
from milo_home.reply_recovery import replay
from milo_home.revisit_control import RevisitControl


def test_new_message_merges_with_old_revisit_without_resending_old_input(client, native_reply_case):
    case, database, _, journal, scenario, intent, _ = deferred(client, native_reply_case)
    case[1][0] = intent["due_at"]
    send(client, case)
    ready(case)
    scenario[0] = "auto-reply-followup"
    assert tick(native_reply_case)["outcome"] == "succeeded"
    run = latest(database)
    with database.snapshot() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM run_messages WHERE run_id=?", (run["id"],)
            ).fetchone()[0]
            == 1
        )
        body = connection.execute(
            "SELECT id FROM messages WHERE run_id=?", (run["id"],)
        ).fetchone()[0]
        assert (
            connection.execute(
                "SELECT count(*) FROM message_reply_links WHERE reply_id=?", (body,)
            ).fetchone()[0]
            == 3
        )
        assert {row[0] for row in connection.execute("SELECT state FROM message_reply_state")} == {
            "replied"
        }
    check = report(case, journal, run)
    assert check["eligible"], check


def test_cancel_after_native_acceptance_keeps_cancellation_and_stops_the_original_family(
    client, native_reply_case, monkeypatch
):
    case, database, owner, journal, scenario, intent, _ = deferred(client, native_reply_case)
    case[1][0] = intent["due_at"]
    scenario[0] = "auto-reply-wait"
    record = NativeReplyDelivery.record
    cancelled = []

    def cancel_after_acceptance(self, event):
        record(self, event)
        if event["kind"] == "response" and event["id"] == self.request_id:
            cancelled.append(
                RevisitControl(database, lambda: case[1][0]).change(
                    owner, intent["id"], 1, action="cancel", no_reply=True
                )
            )

    monkeypatch.setattr(NativeReplyDelivery, "record", cancel_after_acceptance)
    assert tick(native_reply_case)["outcome"] == "cancelled"
    assert cancelled and not journal.outstanding()
    with database.snapshot() as connection:
        assert not execution_busy(connection)
        assert (
            connection.execute(
                "SELECT state FROM revisit_intents WHERE id=?", (intent["id"],)
            ).fetchone()[0]
            == "cancelled"
        )
        assert (
            connection.execute("SELECT count(*) FROM messages WHERE role='milo'").fetchone()[0] == 0
        )
        assert {row[0] for row in connection.execute("SELECT state FROM message_reply_state")} == {
            "no_reply"
        }
        assert (
            connection.execute(
                "SELECT fault_code FROM automatic_services WHERE kind='reply'"
            ).fetchone()[0]
            is None
        )
    before = len(requests(native_reply_case))
    assert tick(native_reply_case)["outcome"] == "waiting_or_other_entry_first"
    assert len(requests(native_reply_case)) == before


def test_revisit_sealed_after_success_only_republishes_and_does_not_rerun(
    client, native_reply_case, monkeypatch
):
    case, database, owner, journal, scenario, intent, _ = deferred(client, native_reply_case)
    case[1][0] = intent["due_at"]
    scenario[0] = "auto-reply-followup"
    with database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_revisit_publication BEFORE INSERT ON message_decisions "
            "BEGIN SELECT RAISE(ABORT,'synthetic outage'); END"
        )
    with pytest.raises(ApiError, match="publication_pending"):
        tick(native_reply_case)
    run = latest(database)
    before = len(requests(native_reply_case))
    with pytest.raises(ApiError, match="publication_pending"):
        RevisitControl(database, lambda: case[1][0]).change(
            owner, intent["id"], 1, action="cancel", no_reply=True
        )
    with database.connect(write=True) as connection:
        connection.execute("DROP TRIGGER fail_revisit_publication")
    case[1][0] += 7200
    monkeypatch.setattr("milo_home.reply_recovery.unit_empty", lambda _: True)
    monkeypatch.setattr("milo_home.native_reply_recovery.stop_family", lambda _: True)
    monkeypatch.setattr(
        "milo_home.reply_recovery.publish_receipt",
        lambda db, who, run_id: publish_receipt(db, who, run_id, clock=lambda: case[1][0]),
    )
    assert replay(database, case[0][4], owner, run["id"])["outcome"] == "succeeded"
    assert replay(database, case[0][4], owner, run["id"])["outcome"] == "succeeded"
    assert len(requests(native_reply_case)) == before
    check = report(case, journal, run)
    assert check["eligible"], check
    with database.snapshot() as connection:
        assert (
            connection.execute(
                "SELECT state FROM revisit_intents WHERE id=?", (intent["id"],)
            ).fetchone()[0]
            == "completed"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM messages WHERE run_id=?", (run["id"],)
            ).fetchone()[0]
            == 1
        )


def test_unknown_stop_never_frees_occupancy_or_revisits_again(
    client, native_reply_case, monkeypatch
):
    case, database, _, _, scenario, intent, _ = deferred(client, native_reply_case)
    case[1][0] = intent["due_at"]
    scenario[0] = "auto-reply-followup"
    unit_type = native_reply_case[4][0]
    stop = unit_type.stop

    def unknown_stop(self):
        stop(
            self
        )  # Stop the synthetic family for real; report an unconfirmed result to the consumer.
        return False

    monkeypatch.setattr(unit_type, "stop", unknown_stop)
    with pytest.raises(ApiError, match="needs_reconciliation"):
        tick(native_reply_case)
    with database.snapshot() as connection:
        assert execution_busy(connection)
        assert (
            connection.execute(
                "SELECT state FROM revisit_intents WHERE id=?", (intent["id"],)
            ).fetchone()[0]
            == "needs_reconciliation"
        )
        assert (
            connection.execute("SELECT count(*) FROM messages WHERE role='milo'").fetchone()[0] == 0
        )
    before = len(requests(native_reply_case))
    assert tick(native_reply_case)["outcome"] == "service_disabled_or_faulted"
    assert len(requests(native_reply_case)) == before
    assert native_reply_case[4][1][-1].process.poll() is not None

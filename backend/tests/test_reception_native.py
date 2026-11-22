"""Reception choices through the native consumer, sealed replay and immutable history."""

import pytest
from test_journal_retention_scope import reply_report
from test_native_reply_execution import native_reply_case as native_reply_case
from test_native_reply_execution import requests, tick
from test_reception import request
from test_reply_execution import latest, ready, send
from test_reply_readiness import preparation as preparation
from test_work_process import fake_units as fake_units

from milo_home.errors import ApiError
from milo_home.reception import current, save
from milo_home.reply_receipts import publish_receipt
from milo_home.reply_recovery import replay
from milo_home.work_tasks import WorkTasks


def test_native_result_really_controls_intake_without_changing_archived_birth(
    client, native_reply_case
):
    case, _, _, scenario, _ = native_reply_case
    database, owner, *_ = case[0]
    scenario[0] = "auto-reply-reception"
    send(client, case)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    before = reply_report(native_reply_case)
    assert before["eligible"], before
    with database.snapshot() as connection:
        shared = current(connection, owner, "reply")["revision"]
        assert current(connection, owner, "work")["revision"] == shared
    count = len(requests(native_reply_case))
    send(client, case)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "reception_closed"
    assert len(requests(native_reply_case)) == count
    with database.connect(write=True) as connection:
        assert (
            connection.execute(
                "SELECT fault_code FROM automatic_services WHERE kind='reply'"
            ).fetchone()[0]
            is None
        )
        save(connection, owner, request("reply", "available", None), case[1][0])
    after = reply_report(native_reply_case)
    assert after["eligible"] and before["business_digest"] == after["business_digest"]
    # Missing original evidence is not replaced with today's effective arrangement.
    with database.connect(write=True) as connection:
        connection.execute("DELETE FROM reception_slots WHERE revision=?", (shared,))
        connection.execute("DELETE FROM reception_changes WHERE origin_run_id IS NOT NULL")
    assert reply_report(native_reply_case)["blocked_by"] == ["reply_message_projection_conflict"]


def test_late_native_replay_preserves_later_control_and_does_not_start_another_turn(
    client, native_reply_case, monkeypatch
):
    case, _, _, scenario, _ = native_reply_case
    database, owner, _, _, directory, *_ = case[0]
    scenario[0] = "auto-reply-reception"
    send(client, case)
    ready(case)
    with database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_reception BEFORE INSERT ON reception_changes "
            "BEGIN SELECT RAISE(ABORT,'publication outage'); END"
        )
    with pytest.raises(ApiError, match="publication_pending"):
        tick(native_reply_case)
    run = latest(database)
    count = len(requests(native_reply_case))
    with database.connect(write=True) as connection:
        connection.execute("DROP TRIGGER fail_reception")
        saved = save(connection, owner, request("work", "focusing"), case[1][0])
    case[1][0] += 120
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
        assert current(connection, owner, "work")["revision"] == saved
        assert current(connection, owner, "reply") is None
        assert (
            connection.execute(
                "SELECT applied FROM reception_changes WHERE origin_run_id=?", (run["id"],)
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM message_decisions").fetchone()[0] == 1


def test_resting_work_does_not_starve_open_reply_service(client, native_reply_case):
    case = native_reply_case[0]
    database, owner, *_ = case[0]
    with database.snapshot() as connection:
        session = connection.execute("SELECT id FROM work_sessions").fetchone()[0]
    from uuid import uuid4

    task, _ = WorkTasks(database, lambda: case[1][0]).create(
        owner, session, "合成更早的交办", str(uuid4())
    )
    case[1][0] += 1
    case[2].configure("work", owner, "milo-home-work.timer", session=session, enabled=True)
    case[2].observe("work", owner, timer_active=True)
    with database.connect(write=True) as connection:
        save(connection, owner, request("work"), case[1][0])
    send(client, case)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    assert WorkTasks(database).get(owner, task["id"])["status"] == "queued"

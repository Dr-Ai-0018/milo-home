"""Admission never repairs, publishes, settles, launches or reauthorizes history."""

import os
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_native_reply_execution import native_reply_case as native_reply_case
from test_native_reply_execution import tick
from test_reply_execution import ready, send
from test_reply_readiness import preparation as preparation
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.errors import ApiError
from milo_home.execution_lock import execution_lock
from milo_home.journal_retention_scope import inspect_stream
from milo_home.native_policy import NativePolicyStore
from milo_home.work_runtime import WorkRuntime

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Host lease requires root")


def work_report(launch_case):
    case, runtime, journal, *_ = launch_case
    stream_id = journal.list_streams()[0]["stream"]["id"]
    with execution_lock(case[0][0]) as lease:
        return inspect_stream(runtime, stream_id, lease=lease)


def successful_work(launch_case):
    case, _, _, launcher, *_ = launch_case
    task, window = reserve(case)
    assert (
        launcher.run(case[0][1], window["id"], credential="synthetic-secret").outcome == "completed"
    )
    return task, window


def reply_report(native_reply_case, *, directory=True):
    case, journal, *_ = native_reply_case
    database, _, _, _, reply_directory, *_ = case[0]
    runtime = WorkRuntime(database, journal, lambda: case[1][0])
    stream_id = journal.list_streams()[0]["stream"]["id"]
    with execution_lock(database) as lease:
        return inspect_stream(
            runtime,
            stream_id,
            lease=lease,
            reply_directory=reply_directory if directory else None,
        )


def successful_reply(native_reply_case, client):
    send(client, native_reply_case[0])
    ready(native_reply_case[0])
    assert tick(native_reply_case)["outcome"] == "succeeded"


def test_successful_work_has_exact_historical_identity_and_stable_digest(launch_case, fake_units):
    case, _, journal, *_ = launch_case
    task, window = successful_work(launch_case)
    before = journal.stream_snapshot(journal.list_streams()[0]["stream"]["id"])
    report = work_report(launch_case)
    assert report["eligible"] and report["blocked_by"] == []
    assert report["scope"]["task_id"] == task["id"]
    assert report["scope"]["window_id"] == window["id"]
    assert report["scope"]["process_identity"]["main_pid"] == fake_units[1][0].main_pid
    assert report["scope"]["process_identity"]["invocation"] == "a" * 32
    assert report["scope"]["fresh_observation"] is False
    stop = dict(before["entries"])[report["scope"]["stop_sequence"]]
    assert stop == report["scope"]["stop"]
    assert len(report["business_digest"]) == 64
    create(case[0])
    case[0][4].revoke(case[0][1])
    after = work_report(launch_case)
    assert after["eligible"] and after["business_digest"] == report["business_digest"]
    assert journal.stream_snapshot(before["stream"]["id"]) == before


def test_work_projection_failure_is_not_repaired_by_admission(launch_case, fake_units):
    case, _, _, launcher, *_ = launch_case
    _, window = reserve(case)
    with case[0][0].connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER retention_fail_projection BEFORE INSERT ON work_results "
            "BEGIN SELECT RAISE(ABORT,'synthetic projection outage'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    report = work_report(launch_case)
    assert report["blocked_by"] == ["work_result_projection_incomplete"]
    with case[0][0].snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM work_results").fetchone()[0] == 0


def test_work_missing_receipt_blocks_even_with_closed_window(launch_case, fake_units):
    successful_work(launch_case)
    with launch_case[2]._connect() as connection:
        connection.execute(
            "DELETE FROM entries WHERE json_extract(payload,'$.kind')='work_terminal'"
        )
    assert work_report(launch_case)["blocked_by"] == ["work_receipts_incomplete"]


def test_work_pending_attachments_block(launch_case, fake_units):
    task, _ = successful_work(launch_case)
    with launch_case[0][0][0].connect(write=True) as connection:
        connection.execute(
            "INSERT INTO work_attachment_batches VALUES (?,'pending',0)", (task["id"],)
        )
    assert work_report(launch_case)["blocked_by"] == ["work_attachments_pending"]


def test_preparation_only_is_explicitly_blocked(launch_case, fake_units):
    case, _, _, launcher, *_ = launch_case
    _, window = reserve(case)
    with launcher.open(case[0][1], window["id"], credential="synthetic-secret"):
        pass
    assert work_report(launch_case)["blocked_by"] == ["no_successful_model_execution"]


def test_work_missing_ready_and_remaining_credential_block(launch_case, fake_units):
    successful_work(launch_case)
    journal = launch_case[2]
    stream_id = journal.list_streams()[0]["stream"]["id"]
    credential = journal.path.parent / (stream_id + ".env")
    credential.symlink_to(journal.path.parent / "absent")
    assert work_report(launch_case)["blocked_by"] == ["temporary_credentials_present"]
    credential.unlink()
    with journal._connect() as connection:
        connection.execute("DELETE FROM entries WHERE json_extract(payload,'$.phase')='ready'")
    assert not work_report(launch_case)["eligible"]


def test_successful_native_reply_requires_original_route_and_ignores_new_policy(
    native_reply_case, client
):
    successful_reply(native_reply_case, client)
    case, journal, *_ = native_reply_case
    before = reply_report(native_reply_case)
    assert before["eligible"] and before["scope"]["kind"] == "native_reply"
    assert reply_report(native_reply_case, directory=False)["blocked_by"] == [
        "native_route_unavailable"
    ]
    store = NativePolicyStore(journal)
    policy = store.read()
    with execution_lock(case[0][0]):
        store.configure(
            owner=str(policy.owner_id),
            session=str(policy.session_id),
            model="another-supported-model",
            expected=policy.digest,
            now=case[1][0] + 1,
        )
    after = reply_report(native_reply_case)
    assert after["eligible"] and after["business_digest"] == before["business_digest"]
    assert after["scope"]["policy_digest"] == policy.digest


def test_native_publication_pending_is_not_published_by_admission(native_reply_case, client):
    case = native_reply_case[0]
    database = case[0][0]
    send(client, case)
    ready(case)
    with database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER retention_fail_publish BEFORE INSERT ON messages "
            "WHEN NEW.role='milo' BEGIN SELECT RAISE(ABORT,'synthetic outage'); END"
        )
    with pytest.raises(ApiError, match="publication_pending"):
        tick(native_reply_case)
    assert reply_report(native_reply_case)["blocked_by"] == ["reply_publication_incomplete"]
    with database.snapshot() as connection:
        assert (
            connection.execute("SELECT count(*) FROM messages WHERE role='milo'").fetchone()[0] == 0
        )
        assert connection.execute("SELECT status FROM runs").fetchone()[0] == "running"


def test_native_published_body_conflict_is_not_hidden(native_reply_case, client):
    successful_reply(native_reply_case, client)
    with native_reply_case[0][0][0].connect(write=True) as connection:
        connection.execute("UPDATE messages SET body='changed projection' WHERE role='milo'")
    assert reply_report(native_reply_case)["blocked_by"] == ["reply_message_projection_conflict"]


def test_native_other_message_does_not_change_this_stream_digest(native_reply_case, client):
    successful_reply(native_reply_case, client)
    before = reply_report(native_reply_case)
    send(client, native_reply_case[0])
    after = reply_report(native_reply_case)
    assert before["eligible"] and after["eligible"]
    assert before["business_digest"] == after["business_digest"]


def test_native_unknown_request_remains_blocked(native_reply_case, client):
    successful_reply(native_reply_case, client)
    journal = native_reply_case[1]
    with journal._connect() as connection:
        connection.execute("UPDATE requests SET state='unknown' WHERE method='turn/start'")
    assert reply_report(native_reply_case)["blocked_by"] == ["protocol_requests_incomplete"]


@pytest.mark.parametrize("missing", ["mapping", "delivery"])
def test_native_dispatch_must_cover_every_selected_message(native_reply_case, client, missing):
    case = native_reply_case[0]
    send(client, case, 2)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    assert reply_report(native_reply_case)["eligible"]
    with case[0][0].connect(write=True) as connection:
        table = "wake_run_events" if missing == "mapping" else "wake_event_delivery"
        event = connection.execute(
            "SELECT e.id FROM wake_events e JOIN wake_run_events r ON r.event_id=e.id "
            "WHERE e.kind='message' ORDER BY e.id LIMIT 1"
        ).fetchone()[0]
        connection.execute(f"DELETE FROM {table} WHERE event_id=?", (event,))
    assert reply_report(native_reply_case)["blocked_by"] == ["reply_dispatch_events_unsettled"]


def test_native_dispatch_allows_original_fallback_event(native_reply_case, client):
    case = native_reply_case[0]
    send(client, case)
    ready(case)
    event_id = str(uuid4())
    with case[0][0].connect(write=True) as connection:
        connection.execute(
            "INSERT INTO wake_events(id,event_key,kind,thread_id,scheduled_at,observed_at) "
            "VALUES (?,?,'fallback',?,?,?)",
            (event_id, "synthetic-fallback:" + event_id, case[0][2], case[1][0], case[1][0]),
        )
    assert tick(native_reply_case)["outcome"] == "succeeded"
    assert reply_report(native_reply_case)["eligible"]


def test_wrong_database_lease_does_not_admit_work(launch_case, fake_units, monkeypatch):
    successful_work(launch_case)
    journal = launch_case[2]
    linked = journal.business_binding()
    monkeypatch.setattr(journal, "business_binding", lambda: replace(linked, database_inode=0))
    assert work_report(launch_case)["blocked_by"] == ["business_database_identity_conflict"]

import time

import pytest
from test_native_reply_execution import native_reply_case as native_reply_case
from test_native_reply_execution import requests, tick
from test_reply_execution import latest, ready, send
from test_reply_readiness import preparation as preparation
from test_work_process import fake_units as fake_units

from milo_home.codex_config import private_create
from milo_home.codex_rpc import StdioRpc
from milo_home.errors import ApiError
from milo_home.execution_lock import execution_lock
from milo_home.native_messages import NativeMessageStore
from milo_home.reply_recovery import recover, replay
from milo_home.scheduler_cli import StopRequest
from milo_home.worker import Worker


def lost_acceptance(client, case, monkeypatch):
    case[3][0] = "auto-reply-lost"
    send(client, case[0])
    ready(case[0])

    def limited(*args, **kwargs):
        kwargs["deadline"] = time.monotonic() + 0.3
        return StdioRpc(*args, **kwargs)

    monkeypatch.setattr("milo_home.native_reply_process.StdioRpc", limited)
    with pytest.raises(ApiError, match="needs_reconciliation"):
        tick(case)
    return latest(case[0][0][0])


def test_lost_delivery_cannot_be_released_by_legacy_or_native_recovery(
    client, native_reply_case, monkeypatch
):
    run = lost_acceptance(client, native_reply_case, monkeypatch)
    case, journal, *_ = native_reply_case
    database, owner, _, _, directory, *_ = case[0]
    count = len(requests(native_reply_case))
    with execution_lock(database):
        with pytest.raises(ApiError, match="native_reply_recovery_required"):
            Worker(database, runtime=directory)._recover_locked(run)
    seen = []
    monkeypatch.setattr(
        "milo_home.native_reply_recovery.stop_family",
        lambda identity: seen.append(identity) or True,
    )
    scope, events = NativeMessageStore(journal).inspect(run["id"])
    environment = journal.path.parent / (events[0].stream_id + ".env")
    private_create(environment, b"SYNTHETIC_KEY=stale-test-value\n")
    for _ in range(2):
        with pytest.raises(ApiError, match="needs_reconciliation"):
            recover(database, directory, owner, run["id"])
    assert seen and all(identity["unit"] == run["unit_name"] + ".service" for identity in seen)
    assert not environment.exists()
    assert journal.outstanding() and latest(database)["status"] == "stopping"
    assert len(requests(native_reply_case)) == count
    assert NativeMessageStore(journal).inspect(scope.run_id)[1][-1].stop_confirmed is True


def test_missing_route_never_falls_back_or_changes_occupancy(
    client, native_reply_case, monkeypatch
):
    run = lost_acceptance(client, native_reply_case, monkeypatch)
    case = native_reply_case[0]
    database, owner, _, _, directory, *_ = case[0]
    (directory / run["id"] / "native-reply.json").unlink()
    monkeypatch.setattr(
        "milo_home.native_reply_recovery.stop_family",
        lambda _: pytest.fail("Missing evidence must not stop an unidentified family"),
    )
    with pytest.raises(ApiError, match="needs_reconciliation"):
        recover(database, directory, owner, run["id"])
    assert latest(database)["status"] == "stopping"


def test_changed_process_identity_cannot_release_or_remove_credentials(
    client, native_reply_case, monkeypatch
):
    run = lost_acceptance(client, native_reply_case, monkeypatch)
    case, journal, *_ = native_reply_case
    database, owner, _, _, directory, *_ = case[0]
    events = NativeMessageStore(journal).inspect(run["id"])[1]
    environment = journal.path.parent / (events[0].stream_id + ".env")
    private_create(environment, b"SYNTHETIC_KEY=stale-test-value\n")
    monkeypatch.setattr("milo_home.native_reply_recovery.stop_family", lambda _: False)
    with pytest.raises(ApiError, match="needs_reconciliation"):
        recover(database, directory, owner, run["id"])
    assert environment.exists() and latest(database)["status"] == "stopping"


def test_known_acceptance_and_fresh_stop_allow_failure_settlement_without_relaunch(
    client, native_reply_case, monkeypatch
):
    from milo_home.native_reply_delivery import NativeReplyDelivery

    case, journal, _, scenario, fake = native_reply_case
    database, owner, _, _, directory, *_ = case[0]
    scenario[0] = "auto-reply-wait"
    fake[0].stop_confirmed = False
    stop = StopRequest()
    record = NativeReplyDelivery.record

    def cancel(self, event):
        record(self, event)
        if event["kind"] == "response" and event["id"] == self.request_id:
            stop.set()

    monkeypatch.setattr(NativeReplyDelivery, "record", cancel)
    send(client, case)
    ready(case)
    with pytest.raises(ApiError, match="needs_reconciliation"):
        tick(native_reply_case, stop=stop)
    run = latest(database)
    count = len(requests(native_reply_case))
    monkeypatch.setattr("milo_home.native_reply_recovery.stop_family", lambda _: True)
    assert (
        recover(database, directory, owner, run["id"])["outcome"] == "reconciled_without_relaunch"
    )
    assert latest(database)["status"] == "cancelled" and not journal.outstanding()
    assert len(requests(native_reply_case)) == count
    assert client.get("/api/threads").json()[0]["pending_count"] == 1


def test_real_native_family_can_be_rechecked_for_publication_only(
    client, native_reply_case, monkeypatch
):
    from milo_home.codex_process import DirectUnit

    monkeypatch.setattr("milo_home.native_reply_process.DirectUnit", DirectUnit)
    case, journal, *_ = native_reply_case
    database, owner, _, _, directory, *_ = case[0]
    send(client, case)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    run = latest(database)
    count = len(requests(native_reply_case))
    assert replay(database, directory, owner, run["id"])["outcome"] == "succeeded"
    assert len(requests(native_reply_case)) == count and not journal.outstanding()

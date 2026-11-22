from uuid import uuid4

import pytest
from test_reply_readiness import preparation as preparation
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.automatic_queue import automatic_choice
from milo_home.automatic_services import SERVICE_TTL, AutomaticServices
from milo_home.dispatch import DispatchStore
from milo_home.errors import ApiError
from milo_home.management_lease import current_boot
from milo_home.messages import MessageService
from milo_home.reply_scope import ReplyScope
from milo_home.service_observation import reply_service_view, timer_active
from milo_home.wakeup_events import EventPlanner


def service_row(database, kind="reply"):
    with database.snapshot() as connection:
        return connection.execute(
            "SELECT * FROM automatic_services WHERE kind=?", (kind,)
        ).fetchone()


def test_control_and_fresh_timer_evidence_are_independent(preparation):
    database, owner, _, schedule, *_ = preparation
    control = AutomaticServices(database, schedule.clock)
    control.configure("reply", owner, "milo-home-reply.timer", enabled=True)
    now = int(schedule.clock())
    assert reply_service_view(service_row(database), owner, now, current_boot()).state == "unknown"
    control.observe("reply", owner, timer_active=False)
    assert reply_service_view(service_row(database), owner, now, current_boot()).state == "unknown"
    control.observe("reply", owner, timer_active=True)
    assert (
        reply_service_view(service_row(database), owner, now, current_boot()).state == "scheduled"
    )
    assert (
        reply_service_view(service_row(database), str(uuid4()), now, current_boot()).state
        == "unknown"
    )
    expired = reply_service_view(service_row(database), owner, now + SERVICE_TTL, current_boot())
    assert expired.state == "unknown" and expired.reason == "observation_expired"
    assert reply_service_view(service_row(database), owner, now, "other-boot").state == "unknown"
    assert (
        reply_service_view(service_row(database), owner, now - 1, current_boot()).state == "unknown"
    )


def test_fault_is_not_cleared_by_checks_or_reconfiguration(preparation):
    database, owner, _, schedule, *_ = preparation
    control = AutomaticServices(database, schedule.clock)
    control.configure("reply", owner, "milo-home-reply.timer", enabled=True)
    control.fault("reply", owner, "execution_failed")
    control.observe("reply", owner, timer_active=True)
    view = reply_service_view(service_row(database), owner, int(schedule.clock()), current_boot())
    assert view.state == "faulted"
    with pytest.raises(ApiError, match="service_faulted"):
        control.configure("reply", owner, "milo-home-reply.timer", enabled=True)
    control.disable("reply", owner)
    assert service_row(database)["enabled"] == 0
    assert service_row(database)["fault_code"] == "execution_failed"
    control.resume("reply", owner)
    assert service_row(database)["fault_code"] is None
    assert service_row(database)["checked_at"] is None


def test_scoped_discovery_reservation_and_launch_do_not_cross_accounts(preparation):
    database, owner, thread, schedule, *_ = preparation
    services = AutomaticServices(database, schedule.clock)
    services.configure("reply", owner, "milo-home-reply.timer", enabled=True)
    other, other_thread = (str(uuid4()) for _ in range(2))
    with database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO users VALUES (?,?,?,'unused',1)", (other, "other", "另一位")
        )
        connection.execute("INSERT INTO threads VALUES (?,?,1)", (other_thread, other))
    for user, selected_thread in [(owner, thread), (other, other_thread)]:
        message, _ = MessageService(database).post(user, selected_thread, "合成留言", str(uuid4()))
        with database.connect(write=True) as connection:
            connection.execute("UPDATE messages SET created_at=1 WHERE id=?", (message["id"],))
    now = [int(schedule.clock())]
    scope = ReplyScope(owner)
    EventPlanner(database, lambda: now[0], scope=scope).plan()
    now[0] += 15
    dispatch = DispatchStore(database, lambda: now[0], scope=scope)
    ticket = dispatch.reserve(60)
    assert ticket is not None
    with database.snapshot() as connection:
        assert {row[0] for row in connection.execute("SELECT thread_id FROM wake_events")} == {
            thread
        }
        assert connection.execute("SELECT owner_id FROM reply_run_scopes").fetchone()[0] == owner
    # Simulate an out-of-band identity change before launch: fail closed in the guard.
    with database.connect(write=True) as connection:
        connection.execute("UPDATE automatic_services SET owner_id=? WHERE kind='reply'", (other,))
    assert not dispatch.authorize_launch(ticket)


def test_shared_election_is_stable_and_disabled_or_stale_peers_do_not_block(work_case):
    database, owner, _, _, _, _, session = work_case
    now = [200]
    control = AutomaticServices(database, lambda: now[0])
    control.configure("work", owner, "milo-home-work.timer", session=session["id"], enabled=True)
    control.configure("reply", owner, "milo-home-reply.timer", enabled=True)
    control.observe("reply", owner, timer_active=True)
    task = create(work_case)
    candidates = [{"ready_at": 100, "message_ids": ["synthetic-first-message"]}]

    def choice(kind):
        with database.connect(write=True) as connection:
            return automatic_choice(
                connection,
                kind,
                owner,
                now[0],
                current_boot(),
                session=session["id"] if kind == "work" else None,
                reply_candidates=candidates,
            )

    control.observe("work", owner, timer_active=True)
    assert choice("reply")["kind"] == choice("work")["kind"] == "reply"
    candidates[0]["ready_at"] = task["created_at"] + 1
    assert choice("reply")["kind"] == choice("work")["kind"] == "work"
    control.disable("work", owner)
    assert choice("reply")["kind"] == "reply"
    assert choice("work") is None
    control.resume("work", owner)
    assert choice("reply")["kind"] == "reply"  # no new timer evidence yet
    control.observe("work", owner, timer_active=True)
    now[0] += SERVICE_TTL
    assert choice("reply")["kind"] == "reply"  # stale peer cannot hold the queue forever
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM execution_windows").fetchone()[0] == 0


def test_peer_reply_with_future_message_does_not_block_valid_work(work_case):
    database, owner, _, _, _, _, session = work_case
    now = 200
    control = AutomaticServices(database, lambda: now)
    control.configure("work", owner, "milo-home-work.timer", session=session["id"], enabled=True)
    control.configure("reply", owner, "milo-home-reply.timer", enabled=True)
    control.observe("reply", owner, timer_active=True)
    create(work_case)
    with database.snapshot() as connection:
        thread = ReplyScope(owner).thread_id(connection)
    message, _ = MessageService(database).post(owner, thread, "合成时钟异常", str(uuid4()))
    with database.connect(write=True) as connection:
        connection.execute("UPDATE messages SET created_at=? WHERE id=?", (now + 1, message["id"]))
        assert (
            automatic_choice(
                connection,
                "work",
                owner,
                now,
                current_boot(),
                session=session["id"],
                reply_candidates=[{"ready_at": 1, "message_ids": [message["id"]]}],
            )["kind"]
            == "work"
        )


def test_timer_evidence_requires_the_expected_service(monkeypatch):
    from types import SimpleNamespace

    def show(output):
        monkeypatch.setattr(
            "milo_home.service_observation.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=output),
        )

    show("LoadState=loaded\nActiveState=active\nTriggers=another.service\n")
    assert not timer_active("milo-home-reply.timer")
    show("LoadState=loaded\nActiveState=active\nTriggers=milo-home-reply.service\n")
    assert timer_active("milo-home-reply.timer")


def test_restored_database_does_not_inherit_launch_authority(preparation):
    from milo_home.automatic_services import require_entry
    from milo_home.database import Database

    database, owner, _, schedule, directory, *_ = preparation
    store = AutomaticServices(database, schedule.clock)
    store.configure("reply", owner, "milo-home-reply.timer", enabled=True)
    store.observe("reply", owner, timer_active=True)
    restored_path = directory / "restored.sqlite3"
    database.backup(restored_path)
    restored = Database(restored_path)
    with (
        restored.snapshot() as connection,
        pytest.raises(ApiError, match="service_database_changed"),
    ):
        require_entry(connection, "reply", owner)
    restored_store = AutomaticServices(restored, schedule.clock)
    restored_store.configure("reply", owner, "milo-home-reply.timer")
    assert service_row(restored)["enabled"] == 0
    assert service_row(database)["enabled"] == 1

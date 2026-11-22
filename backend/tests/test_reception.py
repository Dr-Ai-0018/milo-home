"""Persisted arrangements, independent scope, authentic input recovery and late results."""

import sqlite3
from uuid import uuid4

import pytest
from test_runs import pending

from milo_home.database import Database
from milo_home.interaction_timing import ReceptionRequest
from milo_home.reception import (
    blocking_reason,
    current,
    observe,
    on_new_input,
    save,
    settle_due,
)
from milo_home.reception_results import (
    publish_reception,
    read_reception_projection,
    require_reception_projection,
)


def request(scope="all", mode="resting", resume_on="explicit", until=None):
    return ReceptionRequest(scope=scope, mode=mode, resume_on=resume_on, until=until)


def owner_case(client):
    thread, store = pending(client, 1)
    owner = client.get("/api/auth/me").json()["id"]
    return store.database, owner, thread, store


def test_scopes_atomic_override_and_restart_preserve_effective_slots(client, tmp_path):
    database, owner, _, _ = owner_case(client)
    with database.connect(write=True) as connection:
        shared = save(connection, owner, request(), 100)
        reply = save(connection, owner, request("reply", "focusing"), 101)
        assert current(connection, owner, "work")["revision"] == shared
        assert current(connection, owner, "reply")["revision"] == reply
        save(connection, owner, request("all", "available", None), 102)
        assert all(
            blocking_reason(connection, owner, kind, 102) is None for kind in ("reply", "work")
        )
        save(connection, owner, request("work", "later", "at_time", 200), 103)
    backup = tmp_path / "reception.sqlite3"
    database.backup(backup)
    copied = Database(backup)
    copied.initialize()
    with copied.snapshot() as connection:
        assert blocking_reason(connection, owner, "reply", 150) is None
        assert blocking_reason(connection, owner, "work", 150) == "reception_closed"
        assert blocking_reason(connection, owner, "work", 200) is None
        assert observe(connection, owner, "work", 150, 250).valid_until == 200
        assert observe(connection, owner, "work", 200, 250) is None
    with copied.connect(write=True) as connection:
        settle_due(connection, 200)
        assert observe(connection, owner, "work", 200, 250).mode == "available"
        revision = current(connection, owner, "work")["revision"]
        settle_due(connection, 300)
        assert current(connection, owner, "work")["revision"] == revision


def test_new_input_recovers_its_category_only_and_clock_rewind_is_unknown(client):
    database, owner, _, _ = owner_case(client)
    with database.connect(write=True) as connection:
        revision = save(connection, owner, request(resume_on="new_input"), 100)
        on_new_input(connection, owner, "reply", 99)
        assert current(connection, owner, "reply")["revision"] == revision
        assert blocking_reason(connection, owner, "reply", 99) == "reception_clock_invalid"
        assert observe(connection, owner, "reply", 99, 140) is None
        on_new_input(connection, owner, "reply", 101)
        assert current(connection, owner, "reply")["mode"] == "available"
        assert current(connection, owner, "work")["revision"] == revision
        save(connection, owner, request("reply", resume_on="explicit"), 102)
        on_new_input(connection, owner, "reply", 103)
        assert blocking_reason(connection, owner, "reply", 103) == "reception_closed"


@pytest.mark.parametrize("later_control", [False, True])
def test_late_original_result_never_revives_expired_arrangement_or_overwrites_control(
    client, later_control
):
    database, owner, thread, store = owner_case(client)
    run_id = store.queue(thread, 3600)
    with database.connect(write=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM reception_run_bases WHERE run_id=?", (run_id,)
        ).fetchone()
        run = dict(connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        when = run["created_at"]
        if later_control:
            save(connection, owner, request("work", "focusing"), when + 2)
        original = request("all", "resting", "at_time", when + 60)
        publish_reception(connection, run, original, when + 120, when + 1)
        birth = read_reception_projection(connection, run_id)
        require_reception_projection(original, run, birth, when + 1)
        assert birth[0]["applied"] == int(not later_control)
        if later_control:
            assert current(connection, owner, "reply") is None
            assert current(connection, owner, "work")["mode"] == "focusing"
        else:
            assert all(
                current(connection, owner, kind)["mode"] == "available"
                for kind in ("reply", "work")
            )
        # Later independent choice does not change original replay evidence.
        save(connection, owner, request(), when + 121)
        assert read_reception_projection(connection, run_id) == birth
        require_reception_projection(original, run, birth, when + 1)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE reception_changes SET mode='later' WHERE origin_run_id=?", (run_id,)
            )


def test_input_arriving_during_original_turn_prevents_stale_new_input_rest(client):
    database, owner, thread, store = owner_case(client)
    run_id = store.queue(thread, 3600)
    response = client.post(
        f"/api/threads/{thread}/messages",
        headers={"Idempotency-Key": str(uuid4())},
        json={"body": "新的一句"},
    )
    assert response.status_code == 201
    with database.connect(write=True) as connection:
        run = dict(connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        now = run["created_at"]
        publish_reception(connection, run, request("reply", resume_on="new_input"), now, now)
        assert current(connection, owner, "reply")["mode"] == "available"


def test_failed_second_slot_rolls_back_the_all_update(client):
    database, owner, _, _ = owner_case(client)
    with database.connect(write=True) as connection:
        save(connection, owner, request(), 100)
        connection.execute(
            "CREATE TRIGGER fail_slot BEFORE UPDATE ON reception_slots "
            "WHEN NEW.kind='work' BEGIN SELECT RAISE(ABORT,'outage'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="outage"):
        with database.connect(write=True) as connection:
            save(connection, owner, request("all", "available", None), 101)
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM reception_changes").fetchone()[0] == 1
        assert all(current(connection, owner, kind)["revision"] == 1 for kind in ("reply", "work"))

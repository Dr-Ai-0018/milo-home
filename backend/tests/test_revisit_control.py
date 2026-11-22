"""Revision-controlled revisit changes preserve the original decisions and active occupancy."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_reply_choices import mixed_case

from milo_home.cli import main
from milo_home.errors import ApiError
from milo_home.execution_control import execution_busy
from milo_home.reply_projection_evidence import read_projection
from milo_home.revisit_claims import claim, claim_current, read_claim
from milo_home.revisit_control import RevisitControl


def setup(client):
    thread, store, run, conversation, ids, result, now = mixed_case(client)
    assert store.publish(run, result)
    database = store.database
    with database.snapshot() as connection:
        revisit = dict(
            connection.execute("SELECT * FROM revisit_intents WHERE origin_index=2").fetchone()
        )
        owner = connection.execute("SELECT owner_id FROM threads WHERE id=?", (thread,)).fetchone()[
            0
        ]
        birth = read_projection(connection, run)
    return (
        database,
        owner,
        revisit,
        RevisitControl(database, lambda: now + 1),
        conversation,
        ids,
        run,
        birth,
    )


def test_cancel_requires_explicit_disposition_and_does_not_create_fake_body(client):
    database, owner, revisit, control, conversation, ids, original, birth = setup(client)
    with pytest.raises(ApiError, match="revisit_disposition_required"):
        control.change(owner, revisit["id"], 1, action="cancel")
    saved = control.change(owner, revisit["id"], 1, action="cancel", no_reply=True)
    assert saved["state"] == "cancelled" and saved["revision"] == 2
    messages = client.get(f"/api/conversations/{conversation}/messages").json()["messages"]
    target = next(item for item in messages if item["id"] == ids[3])
    assert target["reply_decision"]["state"] == "no_reply"
    assert target["reply_decision"]["revision"] == 2
    assert target["reply_decision"]["revisit"] is None
    assert sum(item["role"] == "milo" for item in messages) == 2
    with database.snapshot() as connection:
        assert read_projection(connection, original) == birth
        assert (
            connection.execute("SELECT count(*) FROM message_control_decisions").fetchone()[0] == 1
        )
    with pytest.raises(ApiError, match="revisit_changed"):
        control.change(owner, revisit["id"], 1, action="cancel", no_reply=True)


def test_reschedule_has_one_winner_and_preserves_immutable_initial_time(client):
    database, owner, revisit, control, *_ = setup(client)
    due = revisit["due_at"] + 3600

    def change():
        try:
            return control.change(owner, revisit["id"], 1, action="reschedule", due_at=due)
        except ApiError as error:
            assert error.code == "revisit_changed"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: change(), range(2)))
    assert sum(item is not None for item in results) == 1
    with database.snapshot() as connection:
        saved = dict(
            connection.execute(
                "SELECT * FROM revisit_intents WHERE id=?", (revisit["id"],)
            ).fetchone()
        )
        assert saved["due_at"] == due and saved["initial_due_at"] == revisit["initial_due_at"]
        assert saved["revision"] == 2 and saved["state"] == "scheduled"
    with pytest.raises(ApiError, match="revisit_not_found"):
        control.change(str(uuid4()), revisit["id"], 2, action="cancel", no_reply=True)


def reserve_synthetic(database, revisit):
    identity = str(uuid4())
    with database.connect(write=True) as connection:
        thread = connection.execute(
            "SELECT thread_id FROM conversations WHERE id=?", (revisit["conversation_id"],)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO runs "
            "(id,thread_id,conversation_id,status,created_at,deadline_at,unit_name) "
            "VALUES (?,?,?,'queued',?,?,?)",
            (
                identity,
                thread,
                revisit["conversation_id"],
                revisit["due_at"],
                revisit["due_at"] + 3600,
                "milo-reply-" + identity,
            ),
        )
        claim(connection, identity, revisit["id"], revisit["revision"], revisit["due_at"])
        connection.execute(
            "INSERT INTO reply_run_scopes VALUES (?,?)", (identity, revisit["owner_id"])
        )
    return identity


def test_cancel_running_requests_stop_but_keeps_occupancy_and_claim_birth(client):
    database, owner, revisit, control, *_ = setup(client)
    running = reserve_synthetic(database, revisit)
    with database.snapshot() as connection:
        birth = read_claim(connection, running)
        assert claim_current(connection, running)
        assert birth["sources"][0]["decision_state"] == "deferred"
    control.clock = lambda: revisit["due_at"] + 1
    control.change(owner, revisit["id"], 1, action="cancel", no_reply=True)
    with database.snapshot() as connection:
        assert not claim_current(connection, running) and execution_busy(connection)
        assert read_claim(connection, running) == birth
        row = connection.execute("SELECT * FROM runs WHERE id=?", (running,)).fetchone()
        assert row["status"] == "stopping" and row["stop_reason"] == "cancelled"


def test_sealed_pending_revisit_must_be_recovered_before_control(client):
    database, owner, revisit, control, *_ = setup(client)
    running = reserve_synthetic(database, revisit)
    with database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO reply_execution_results VALUES (?,?,'synthetic','{}')",
            (running, revisit["due_at"]),
        )
    control.clock = lambda: revisit["due_at"] + 1
    with pytest.raises(ApiError, match="publication_pending"):
        control.change(owner, revisit["id"], 1, action="cancel", no_reply=True)
    with database.snapshot() as connection:
        assert claim_current(connection, running)
        assert connection.execute("SELECT count(*) FROM revisit_controls").fetchone()[0] == 0


def test_local_cli_lists_and_explicitly_cancels_without_launching(client, monkeypatch, capsys):
    database, owner, revisit, _, _, _, original, birth = setup(client)
    monkeypatch.setenv("MILO_DATABASE", str(database.path))
    monkeypatch.setattr(sys, "argv", ["milo-home", "revisit", "show", "--owner", owner])
    main()
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2 and {item["state"] for item in rows} == {"scheduled"}
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "milo-home",
            "revisit",
            "cancel",
            "--owner",
            owner,
            "--id",
            revisit["id"],
            "--revision",
            "1",
            "--no-reply",
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["state"] == "cancelled"
    with database.snapshot() as connection:
        assert read_projection(connection, original) == birth
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1

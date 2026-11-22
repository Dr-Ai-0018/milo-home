import json
import os
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest
from conftest import activate, own_thread
from test_schedules import configured

from milo_home.errors import ApiError
from milo_home.execution_budget import ExecutionBudget, ExecutionPolicy
from milo_home.execution_lock import execution_lock
from milo_home.reply_readiness import check_reply_service
from milo_home.reply_scope import ReplyScope

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Independent preparation requires root")


@pytest.fixture
def preparation(client, private_runtime_directory):
    activate(client)
    thread = own_thread(client)
    database = client.app.state.database
    with database.snapshot() as connection:
        owner = connection.execute("SELECT owner_id FROM threads WHERE id=?", (thread,)).fetchone()[
            0
        ]
    schedule = configured(client, "10:00")
    ExecutionBudget(database, schedule.clock).configure(
        ExecutionPolicy(daily_launches=200, daily_seconds=None)
    )
    directory = private_runtime_directory("milo-reply-preflight-")
    profile = directory / "newawz.config.toml"
    profile.write_text(
        'model="synthetic-model"\nmodel_provider="synthetic"\n'
        '[model_providers.synthetic]\nname="test"\n'
        'base_url="https://example.invalid"\nenv_key="MILO_SYNTHETIC_KEY"\nwire_api="responses"\n'
    )
    profile.chmod(0o600)
    key = directory / "provider.key"
    key.write_text("synthetic-private-file-key\n")
    key.chmod(0o600)
    return database, owner, thread, schedule, directory, profile, key


def inspect(case):
    database, owner, _, schedule, _, profile, key = case
    return check_reply_service(database, owner, profile, key, clock=schedule.clock)


def content(database):
    with database.snapshot() as connection:
        return list(connection.iterdump())


def test_readiness_is_pure_read_with_existing_policy_and_explicit_credential(preparation):
    database, _, _, _, directory, _, _ = preparation
    before = content(database)
    files = {path: path.read_bytes() for path in directory.iterdir()}
    report = inspect(preparation)
    assert report["ready"] and report["blockers"] == []
    assert report["requested_timeout"] == 3600
    assert report["budget"]["remaining_launches"] == 200
    assert report["budget"]["remaining_seconds"] is None
    assert report["execution_available"] is False
    assert report["automatic_execution_requested"] is False
    assert content(database) == before
    assert {path: path.read_bytes() for path in directory.iterdir()} == files
    assert "synthetic-private-file-key" not in json.dumps(report)


@pytest.mark.parametrize("mode", ["missing", "public", "linked", "multiline", "wrong_profile"])
def test_invalid_file_never_falls_back_to_environment(preparation, monkeypatch, mode):
    _, _, _, _, directory, profile, key = preparation
    monkeypatch.setenv("MILO_SYNTHETIC_KEY", "environment-must-not-be-used")
    if mode == "missing":
        key.unlink()
    elif mode == "public":
        key.chmod(0o644)
    elif mode == "linked":
        key.rename(directory / "original.key")
        key.symlink_to(directory / "original.key")
    elif mode == "multiline":
        key.write_text("synthetic\ninvalid\n")
    else:
        profile.write_text("not toml = [")
    report = inspect(preparation)
    assert not report["ready"]
    assert report["blockers"] == [
        "profile_invalid" if mode == "wrong_profile" else "credential_invalid"
    ]
    assert "environment-must-not-be-used" not in json.dumps(report)


@pytest.mark.parametrize(
    "phase,reason", [("resting", "schedule_resting"), ("paused", "schedule_paused")]
)
def test_closed_schedule_reports_waiting_without_changing_policy(preparation, phase, reason):
    database, _, _, schedule, _, _, _ = preparation
    if phase == "paused":
        schedule.pause(True)
    else:
        schedule.override(phase, int(schedule.clock()) + 60)
    before = content(database)
    assert inspect(preparation)["blockers"] == [reason]
    assert content(database) == before


def test_account_scope_is_uuid_and_cannot_select_other_thread(preparation, client):
    database, owner, thread, _, _, _, _ = preparation
    with database.snapshot() as connection:
        ReplyScope(owner).require_thread(connection, thread)
        with pytest.raises(ApiError, match="reply_scope_changed"):
            ReplyScope(owner).require_thread(connection, str(uuid4()))
    with pytest.raises(ValueError):
        ReplyScope("visitor")
    activate(client, "other")
    other_thread = own_thread(client)
    response = client.post(
        f"/api/threads/{other_thread}/messages",
        headers={"Idempotency-Key": str(uuid4())},
        json={"body": "another-account-body-must-not-be-read"},
    )
    assert response.status_code == 201
    assert inspect(preparation)["queue"]["pending_count"] == 0
    with pytest.raises(ApiError, match="reply_owner_missing"):
        database, _, _, schedule, _, profile, key = preparation
        check_reply_service(database, str(uuid4()), profile, key, clock=schedule.clock)


def test_missing_policy_and_host_lock_are_not_repaired(preparation):
    database = preparation[0]
    with database.connect(write=True) as connection:
        connection.execute("UPDATE execution_policy SET policy=NULL WHERE id=1")
    before = content(database)
    assert inspect(preparation)["blockers"] == ["execution_policy_missing"]
    assert content(database) == before
    with execution_lock(database), pytest.raises(ApiError, match="worker_busy"):
        inspect(preparation)


def test_existing_finite_manager_is_a_blocker_not_replaced(preparation):
    from milo_home.automatic_budget import AutomaticPolicy
    from milo_home.management_lease import ManagementLease

    database, _, _, schedule, _, _, _ = preparation
    lease = ManagementLease(
        database,
        AutomaticPolicy(daily_launches=200, daily_seconds=None),
        clock=schedule.clock,
    )
    lease.claim()
    try:
        before = content(database)
        assert inspect(preparation)["blockers"] == ["management_busy"]
        assert content(database) == before
    finally:
        lease.close()


def test_shared_manual_launch_consumes_the_same_budget(preparation):
    from milo_home.messages import MessageService
    from milo_home.runs import RunStore

    database, owner, thread, schedule, _, _, _ = preparation
    ExecutionBudget(database, schedule.clock).configure(
        ExecutionPolicy(daily_launches=1, daily_seconds=None)
    )
    message, _ = MessageService(database).post(owner, thread, "合成待回留言", str(uuid4()))
    with database.connect(write=True) as connection:
        connection.execute(
            "UPDATE messages SET created_at=? WHERE id=?", (int(schedule.clock()), message["id"])
        )
    runs = RunStore(database, schedule.clock)
    run_id = runs.queue(thread, 60)
    assert runs.authorize_launch(run_id)
    runs.finish_failed(run_id, "execution_failed")
    before = content(database)
    report = inspect(preparation)
    assert report["blockers"] == ["daily_budget_exhausted"]
    assert report["budget"]["remaining_launches"] == 0
    assert report["queue"]["pending_count"] == 1
    assert content(database) == before


def test_cli_missing_old_database_and_invalid_owner_never_initialize(preparation):
    _, owner, _, _, directory, profile, key = preparation
    target = directory / "not-created.sqlite3"

    def command(*args):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "milo_home.cli",
                "--run-directory",
                str(directory),
                "reply-service",
                "check",
                "--owner",
                owner,
                "--profile",
                str(profile),
                "--credential-file",
                str(key),
                *args,
            ],
            env={**os.environ, "MILO_DATABASE": str(target)},
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert command().returncode == 1 and not target.exists()
    target.touch(mode=0o600)
    with sqlite3.connect(target) as connection:
        connection.execute("PRAGMA user_version=1")
    before = target.read_bytes()
    result = command()
    assert result.returncode == 1 and result.stdout == ""
    assert target.read_bytes() == before
    assert "synthetic-private-file-key" not in result.stdout + result.stderr

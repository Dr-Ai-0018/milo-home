import json
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_execution_control import legacy
from test_work_creation import call, requests, state
from test_work_creation import creation_case as creation_case
from test_work_initialization import init_case as init_case
from test_work_process import fake_units as fake_units
from test_work_setup import invoke
from test_work_tasks import work_case as work_case

from milo_home.codex_config import CodexRuntime, NewawzConfig
from milo_home.database_identity import ServiceDatabase
from milo_home.errors import ApiError
from milo_home.run_queue import queue_run
from milo_home.work_creation_records import ABANDONED, CHOICE, EVENTS, CreationRecords, encoded

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root creation")


def prepare(case, monkeypatch, *, database=None, account=None, scenario="create-eof"):
    fixture_config = case[0][4]
    content = (
        f'model_provider = "{fixture_config.provider}"\nmodel = "{fixture_config.model}"\n'.encode()
        + fixture_config.content
    )
    config = replace(fixture_config, content=content)
    monkeypatch.setattr(NewawzConfig, "load", lambda: config)
    case[3][0] = scenario
    with pytest.raises(SystemExit):
        call(case, monkeypatch, database=database, account=account)
    home = case[0][1]
    with closing(sqlite3.connect(home / "state_37.sqlite")) as connection:
        connection.executescript(
            "CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, "
            "model_provider TEXT, model TEXT); "
            "CREATE TABLE rollout_migration_skipped_rollouts(path TEXT PRIMARY KEY);"
        )
    return home


def abandon(case, monkeypatch, *, database=None, account=None):
    work = case[0][0][0]
    invoke(
        monkeypatch,
        database or work[0],
        case[0][1],
        "abandon-main",
        "--owner",
        work[1],
        "--session",
        work[-1]["id"],
        "--timeout",
        "60",
        "--confirm-stop",
        "--abandon",
        account=account,
    )


def observe(monkeypatch, *, absent=True):
    identities = []

    def sample(identity, *, run):
        identities.append(identity)
        properties = {
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "MainPID": "0",
            "InvocationID": "",
            "ControlGroup": "",
        }
        return {
            "absent_observed": absent,
            "reason": "recorded-family-absent" if absent else "family-active",
            "boot_before": identity["boot_id"],
            "boot_after": identity["boot_id"],
            "systemd_before": properties,
            "systemd_after": properties,
            "cgroup": {"exists": False, "populated": None},
        }

    monkeypatch.setattr("milo_home.work_creation_abandon.observe_family", sample)
    return identities


def evidence(home):
    return {path.name: path.read_bytes() for path in (home / EVENTS).glob("*.json")}


def test_abandon_only_releases_shared_occupancy(
    creation_case, monkeypatch, capsys, private_runtime_directory
):
    work, _, _, now = creation_case[0][0]
    database, owner = work[:2]
    thread, _ = legacy(database, owner)
    home = prepare(creation_case, monkeypatch)
    config = home / "config.toml"
    workspace = json.loads((home / CHOICE).read_bytes())["cwd"]
    trust_writeback = f'\n[projects.{json.dumps(workspace)}]\ntrust_level="trusted"\n'.encode()
    config.write_bytes(config.read_bytes() + trust_writeback)
    original = {
        path: path.read_bytes() for path in (home / CHOICE, config, home / "state_37.sqlite")
    }
    before = evidence(home)
    wire = requests(creation_case)
    identities = observe(monkeypatch)
    monkeypatch.delenv("SYNTHETIC_KEY")

    def forbidden(*args, **kwargs):
        raise AssertionError("Abandonment must not load a live profile or execute Codex")

    monkeypatch.setattr(NewawzConfig, "load", forbidden)
    monkeypatch.setattr(CodexRuntime, "verify", forbidden)
    with database.connect(write=True) as connection, pytest.raises(ApiError, match="run_busy"):
        queue_run(connection, thread, 60, now[0])

    abandon(creation_case, monkeypatch)
    report = json.loads(capsys.readouterr().out)
    assert report["association_recorded"] is False
    assert report["execution_available"] is False
    assert report["execution_occupancy_released"] is True
    assert report["thread_start_delivery"] == "unknown"
    assert state(creation_case) == ("closed", False)
    after = evidence(home)
    assert len(after) == len(before) + 1
    assert {name: after[name] for name in before} == before
    assert {path: path.read_bytes() for path in original} == original
    marker = CreationRecords(home).events[-1]
    assert marker["kind"] == ABANDONED
    assert marker["connection"] == identities[0]["unit"]
    assert not (home / "session-journal.sqlite3").exists()
    assert not (home / "main-registration.json").exists()

    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch, recover=True)
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    elsewhere = private_runtime_directory("milo-abandon-fresh-")
    alternate = ((creation_case[0][0], elsewhere, *creation_case[0][2:]), *creation_case[1:])
    with pytest.raises(SystemExit):
        call(alternate, monkeypatch)
    assert not list(elsewhere.iterdir())
    assert not creation_case[4] and requests(creation_case) == wire

    with pytest.raises(SystemExit):
        abandon(creation_case, monkeypatch)
    assert evidence(home) == after
    with database.connect(write=True) as connection:
        assert not connection.execute("SELECT 1 FROM work_runtime_bindings").fetchone()
        run_id = queue_run(connection, thread, 60, now[0])
        assert connection.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[
            0
        ] == ("queued")
        connection.rollback()
    assert state(creation_case) == ("closed", False)


@pytest.mark.parametrize(
    "change",
    [
        "missing-ready",
        "foreign-connection",
        "active",
        "regrant",
        "changed-config",
        "state-row",
        "state-wal",
        "rollout",
    ],
)
def test_ambiguous_abandonment_does_not_release_or_append(creation_case, monkeypatch, change):
    home = prepare(creation_case, monkeypatch)
    observe(monkeypatch, absent=change != "active")
    if change == "missing-ready":
        (home / EVENTS / "000002.json").unlink()
    elif change == "foreign-connection":
        original = home / EVENTS / "000001.json"
        event = json.loads(original.read_bytes())
        event["connection"] = "milo-work-" + uuid4().hex + ".service"
        original.write_bytes(encoded(event))
    elif change == "regrant":
        work = creation_case[0][0][0]
        work[4].revoke(work[1])
        work[4].grant(work[1])
    elif change == "changed-config":
        config = home / "config.toml"
        config.write_bytes(config.read_bytes() + b'\nunknown_setting="unexpected"\n')
    elif change == "state-row":
        with closing(sqlite3.connect(home / "state_37.sqlite")) as connection, connection:
            connection.execute("INSERT INTO threads(id) VALUES ('synthetic-thread')")
    elif change == "state-wal":
        (home / "state_37.sqlite-wal").touch()
    elif change == "rollout":
        (home / "sessions").mkdir()
    before = evidence(home)
    wire = requests(creation_case)
    with pytest.raises(SystemExit):
        abandon(creation_case, monkeypatch)
    assert evidence(home) == before and requests(creation_case) == wire
    assert state(creation_case) == ("unknown", True)


def test_initialized_or_partially_initialized_creation_cannot_be_abandoned(
    creation_case, monkeypatch
):
    home = prepare(creation_case, monkeypatch, scenario="marker-eof")
    observe(monkeypatch)
    before = evidence(home)
    assert any(item["method"] == "thread/inject_items" for item in requests(creation_case))
    with pytest.raises(SystemExit):
        abandon(creation_case, monkeypatch)
    assert evidence(home) == before and state(creation_case) == ("unknown", True)


def test_committed_audit_survives_failed_service_database_write_and_exact_retry(
    creation_case, monkeypatch, owned_database
):
    path, account = owned_database
    source = creation_case[0][0][0][0]
    seed = path.with_name("seed.sqlite3")
    source.backup(seed)
    os.chown(seed, account.pw_uid, account.pw_gid)
    os.replace(seed, path)
    database = ServiceDatabase(path, account.pw_name)
    options = {"database": database, "account": account.pw_name}
    home = prepare(creation_case, monkeypatch, **options)
    identities = observe(monkeypatch)
    original = ServiceDatabase.connect
    wire = requests(creation_case)

    @contextmanager
    def fail_commit(self, *, write=False):
        with original(self, write=write) as connection:
            yield connection
            if write:
                assert connection.execute("SELECT state FROM work_main_creations").fetchone()[
                    0
                ] == ("closed")
                raise OSError("synthetic commit interrupted")

    monkeypatch.setattr(ServiceDatabase, "connect", fail_commit)
    with pytest.raises(SystemExit):
        abandon(creation_case, monkeypatch, **options)
    assert os.geteuid() == 0
    with original(database) as connection:
        assert connection.execute("SELECT state FROM work_main_creations").fetchone()[0] == (
            "unknown"
        )
    after = evidence(home)
    assert CreationRecords(home).events[-1]["kind"] == ABANDONED
    assert all(path.stat().st_uid == 0 for path in (home / EVENTS).iterdir())
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch, recover=True, **options)
    monkeypatch.setattr(ServiceDatabase, "connect", original)
    abandon(creation_case, monkeypatch, **options)
    assert evidence(home) == after and requests(creation_case) == wire
    assert len(identities) == 2
    with database.connect() as connection:
        assert connection.execute("SELECT state FROM work_main_creations").fetchone()[0] == (
            "closed"
        )
        assert not connection.execute("SELECT 1 FROM work_runtime_bindings").fetchone()

import os
import signal
import time

import pytest
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_creation import call, requests, state
from test_work_creation import creation_case as creation_case
from test_work_initialization import init_case as init_case
from test_work_process import fake_units as fake_units
from test_work_tasks import work_case as work_case

from milo_home.codex_config import CodexRuntime
from milo_home.codex_journal import PrivateJournal
from milo_home.database_identity import ServiceDatabase
from milo_home.execution_lock import execution_lock
from milo_home.work_creation_records import CHOICE, CreationRecords

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root creation")


@pytest.mark.parametrize(
    "failure", ["authority", "credential", "version", "regrant", "busy", "lock", "dirty"]
)
def test_preflight_rejects_without_new_files_or_process(creation_case, monkeypatch, failure):
    initial = creation_case[0]
    work = initial[0][0]
    if failure == "authority":
        work[4].revoke(work[1])
    elif failure == "credential":
        monkeypatch.delenv("SYNTHETIC_KEY")
    elif failure in {"version", "regrant"}:

        def verify(_):
            if failure == "version":
                raise ValueError("synthetic wrong version")
            work[4].revoke(work[1])
            work[4].grant(work[1])

        monkeypatch.setattr(CodexRuntime, "verify", verify)
    elif failure == "busy":
        reserve(initial[0])
    elif failure == "dirty":
        (initial[1] / "existing-evidence").write_bytes(b"preserve")
    before = {p.name: p.read_bytes() for p in initial[1].iterdir()}
    if failure == "lock":
        with execution_lock(work[0]), pytest.raises(SystemExit):
            call(creation_case, monkeypatch)
    else:
        with pytest.raises(SystemExit):
            call(creation_case, monkeypatch)
    assert {p.name: p.read_bytes() for p in initial[1].iterdir()} == before
    assert not creation_case[1] and not requests(creation_case)


@pytest.mark.parametrize("seconds", [0, 3601])
def test_invalid_total_deadline_does_not_create_evidence(creation_case, monkeypatch, seconds):
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch, timeout=seconds)
    assert not list(creation_case[0][1].iterdir()) and not creation_case[1]


@pytest.mark.parametrize("moment", ["launch_intent", "before_registration"])
def test_regrant_cannot_be_crossed_within_one_creation_invocation(
    creation_case, monkeypatch, moment
):
    import milo_home.work_creation as module

    work = creation_case[0][0][0]

    def regrant():
        work[4].revoke(work[1])
        work[4].grant(work[1])

    if moment == "launch_intent":
        original = CreationRecords.record

        def record(self, event):
            original(self, event)
            if event.get("phase") == "launch_intent":
                regrant()

        monkeypatch.setattr(CreationRecords, "record", record)
    else:
        original = module.register_main_locked

        def register(*args, **kwargs):
            regrant()
            return original(*args, **kwargs)

        monkeypatch.setattr(module, "register_main_locked", register)
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    with work[0].connect() as connection:
        assert not connection.execute("SELECT 1 FROM work_runtime_bindings").fetchone()
    assert state(creation_case)[0] == ("unknown" if moment == "launch_intent" else "closed")
    if moment == "launch_intent":
        assert not creation_case[1]
    else:
        monkeypatch.setattr(module, "register_main_locked", original)
        before = requests(creation_case)
        # This explicit invocation accepts current authority, for the same
        # original choice only. It never restarts creation or model work.
        call(creation_case, monkeypatch, recover=True)
        assert requests(creation_case) == before
        assert PrivateJournal(creation_case[0][1], initialize=False).business_binding()


@pytest.mark.parametrize("action", ["signal", "config", "expired"])
def test_changed_prelaunch_checkpoint_keeps_unknown_and_removes_credential(
    creation_case, monkeypatch, action
):
    from milo_home.work_receiver import ReceiveLimit

    original = CreationRecords.record
    wall = [time.time()]
    monkeypatch.setattr(
        "milo_home.work_creation.ReceiveLimit",
        lambda seconds, _: ReceiveLimit(seconds, lambda: wall[0]),
    )

    def record(self, event):
        original(self, event)
        if event.get("phase") != "launch_intent":
            return
        if action == "signal":
            os.kill(os.getpid(), signal.SIGTERM)
        elif action == "config":
            (creation_case[0][1] / "config.toml").write_bytes(b"changed")
        else:
            wall[0] += 60

    monkeypatch.setattr(CreationRecords, "record", record)
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    assert state(creation_case) == ("unknown", True)
    assert not creation_case[1] and not list(creation_case[0][1].glob("*.env"))


@pytest.mark.parametrize("failure", ["ready", "stop"])
def test_incomplete_process_evidence_cannot_release_or_recreate(
    creation_case, fake_units, monkeypatch, failure
):
    if failure == "ready":
        fake_units[0].ready_error = True
    else:
        fake_units[0].stop_confirmed = False
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    assert state(creation_case) == ("unknown", True)
    assert all(u.process.poll() is not None for u in creation_case[1])
    before = requests(creation_case)
    if failure == "ready":
        with pytest.raises(SystemExit):
            call(creation_case, monkeypatch, recover=True)
        assert requests(creation_case) == before
    assert not list(creation_case[0][1].glob("*.env"))


def test_choice_before_failed_claim_is_preserved_but_not_automatically_recoverable(
    creation_case, monkeypatch
):
    from milo_home.work_creation import MainCreation

    def fail(_):
        raise OSError("synthetic preclaim publication interruption")

    monkeypatch.setattr(MainCreation, "_claim", fail)
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    assert state(creation_case) == (None, False)
    choice = (creation_case[0][1] / CHOICE).read_bytes()
    for recovery in [False, True]:
        with pytest.raises(SystemExit):
            call(creation_case, monkeypatch, recover=recovery)
    assert (creation_case[0][1] / CHOICE).read_bytes() == choice
    assert not creation_case[1] and not requests(creation_case)


def test_coordinator_abrupt_exit_preserves_business_occupancy_and_original_responses(
    creation_case, monkeypatch
):
    from milo_home.work_creation import MainCreation

    original = MainCreation._finish

    def abrupt_exit(_):
        os._exit(73)

    monkeypatch.setattr(MainCreation, "_finish", abrupt_exit)
    child = os.fork()
    if child == 0:
        call(creation_case, monkeypatch)
        os._exit(74)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 73
    assert state(creation_case) == ("held", True)
    monkeypatch.setattr(MainCreation, "_finish", original)
    call(creation_case, monkeypatch, recover=True)
    methods = [r["method"] for r in requests(creation_case)]
    assert methods.count("thread/start") == methods.count("thread/inject_items") == 1
    assert methods.count("thread/resume") == 1 and state(creation_case) == ("closed", False)


def test_service_owned_database_keeps_private_runtime_under_root(
    creation_case, owned_database, monkeypatch
):
    work = creation_case[0][0][0]
    path, account = owned_database
    seed = path.parent / "seed.sqlite3"
    work[0].backup(seed)
    os.chown(seed, account.pw_uid, account.pw_gid)
    os.replace(seed, path)
    database = ServiceDatabase(path, account.pw_name)
    call(creation_case, monkeypatch, database=database, account=account.pw_name)
    journal = PrivateJournal(creation_case[0][1], initialize=False)
    assert journal.business_binding().database == str(path)
    assert os.geteuid() == 0
    assert all(p.stat().st_uid == 0 for p in creation_case[0][1].rglob("*"))
    assert all(p.stat().st_uid == account.pw_uid for p in path.parent.iterdir())

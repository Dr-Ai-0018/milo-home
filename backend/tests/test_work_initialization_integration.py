import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_work_coordinator import starts
from test_work_execution import invoke as run
from test_work_initialization import cli_arguments, initialize
from test_work_initialization import init_case as init_case
from test_work_process import fake_units as fake_units
from test_work_setup import invoke
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.codex_config import CodexRuntime
from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.database import Database
from milo_home.database_identity import ServiceDatabase
from milo_home.work_initialization import INTENT
from milo_home.work_process import WorkLauncher
from milo_home.work_progress import WorkProgress
from milo_home.work_runtime import WorkRuntime

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root registration")


def test_empty_metadata_cli_registration_to_original_main_and_public_result(
    init_case, monkeypatch, capsys, fake_units, tmp_path
):
    case, directory, thread, installed, config = init_case
    database = case[0][0]
    invoke(monkeypatch, database, directory, *cli_arguments(init_case))
    assert json.loads(capsys.readouterr().out)["protocol_verified"] is False
    journal = PrivateJournal(directory, initialize=False)
    runtime = WorkRuntime(database, journal, lambda: case[-1][0])
    launcher = WorkLauncher(runtime, config, installed)
    output = tmp_path / "initialized-main.ndjson"
    peer = Path(__file__).parent / "fixtures/codex_app_server.py"
    monkeypatch.setattr(
        CodexRuntime,
        "command",
        lambda _, *, workspace: [sys.executable, str(peer), "normal", str(output)],
    )
    monkeypatch.setattr("milo_home.codex_process.time.time", runtime.clock)
    assembled = case, runtime, journal, launcher, output, ["normal"]
    task = create(case[0])
    run(monkeypatch, assembled, task["id"])
    assert json.loads(capsys.readouterr().out)["outcome"] == "completed"
    assert {request["threadId"] for request in starts(output)} == {thread}
    results = WorkProgress(database).results(case[0][1], task["id"])["results"]
    assert results[0]["output"]["answer"]["body"] == "合成结果"
    assert not journal.outstanding() and not list(directory.glob("*.env"))
    assert len(fake_units[1]) == 1 and fake_units[1][0].process.poll() is not None


def test_service_account_cli_initializes_private_files_only_as_root(
    init_case, owned_database, monkeypatch, capsys
):
    case, directory, _, _, _ = init_case
    path, account = owned_database
    copy = path.parent / "registration-seed.sqlite3"
    case[0][0].backup(copy)
    os.chown(copy, account.pw_uid, account.pw_gid)
    os.replace(copy, path)
    database = ServiceDatabase(path, account.pw_name)
    invoke(monkeypatch, database, directory, *cli_arguments(init_case), account=account.pw_name)
    assert json.loads(capsys.readouterr().out)["association_recorded"]
    assert PrivateJournal(directory, initialize=False).business_binding().database == str(path)
    assert os.geteuid() == 0
    assert all(p.stat().st_uid == 0 for p in directory.iterdir())
    assert all(p.stat().st_uid == account.pw_uid for p in path.parent.iterdir())


@pytest.mark.parametrize("published", [INTENT, "config.toml"])
def test_process_exit_after_publication_resumes_only_original_selection(
    init_case, monkeypatch, published
):
    import milo_home.work_initialization as module

    original = module._publish_file
    child = os.fork()
    if child == 0:

        def exit_after_publish(path, content):
            original(path, content)
            if path.name == published:
                os._exit(73)

        module._publish_file = exit_after_publish
        try:
            initialize(init_case)
        except BaseException:
            os._exit(74)
        os._exit(75)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 73
    committed = (init_case[1] / INTENT).read_bytes()
    with pytest.raises(ProtocolError, match="differs"):
        initialize(init_case, thread_id=str(uuid4()))
    assert initialize(init_case)["association_recorded"] is True
    assert (init_case[1] / INTENT).read_bytes() == committed


@pytest.mark.parametrize("target", [INTENT, "config.toml", "session-journal.sqlite3"])
def test_failed_atomic_link_never_publishes_a_partial_final(init_case, monkeypatch, target):
    original = os.link

    def fail(source, destination, **kwargs):
        if Path(destination).name == target:
            raise OSError("synthetic publication failure")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(OSError, match="publication"):
        initialize(init_case)
    assert not (init_case[1] / target).exists()
    monkeypatch.setattr(os, "link", original)
    assert initialize(init_case)["association_recorded"]


@pytest.mark.parametrize("target", [INTENT, "config.toml", "session-journal.sqlite3-wal"])
def test_damaged_or_orphaned_private_evidence_is_preserved(init_case, target):
    path = init_case[1] / target
    path.write_bytes(b"synthetic damaged evidence")
    path.chmod(0o600)
    with pytest.raises((ProtocolError, ValueError)):
        initialize(init_case)
    assert path.read_bytes() == b"synthetic damaged evidence"
    assert not (init_case[1] / "session-journal.sqlite3").exists()


def test_cross_database_same_home_winner_pins_original_intent(init_case, monkeypatch):
    import milo_home.work_initialization as module

    original = module._publish_file
    copy = init_case[0][0][0].path.with_name("competing-registration.sqlite3")
    init_case[0][0][0].backup(copy)
    competing = Database(copy)
    completed = []

    def interleave(path, content):
        if path.name == INTENT and not completed:
            # Different DB locks do not serialize one private home. Let the
            # competing original inode win just before this atomic publish.
            completed.append(True)
            initialize(init_case, database=competing)
        return original(path, content)

    monkeypatch.setattr(module, "_publish_file", interleave)
    with pytest.raises(ProtocolError, match="differs"):
        initialize(init_case)
    intent = json.loads((init_case[1] / INTENT).read_bytes())
    assert intent["database"] == str(copy)
    journal = PrivateJournal(init_case[1], initialize=False)
    assert journal.business_binding().database == str(copy)
    with init_case[0][0][0].connect() as connection:
        assert connection.execute("SELECT count(*) FROM work_runtime_bindings").fetchone()[0] == 0

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_work_creation import call, requests
from test_work_creation import creation_case as creation_case
from test_work_creation_abandon import abandon, observe, prepare
from test_work_creation_native import CLI, TOOL_HOST, VERIFY_INSTALLED
from test_work_initialization import init_case as init_case
from test_work_process import fake_units as fake_units
from test_work_setup import invoke
from test_work_tasks import work_case as work_case

from milo_home.codex_config import CodexRuntime, NewawzConfig
from milo_home.codex_journal import PrivateJournal
from milo_home.database_identity import ServiceDatabase
from milo_home.execution_control import execution_busy
from milo_home.work_attempt_history import attempt_rows, verify_history
from milo_home.work_creation_process import DirectUnit
from milo_home.work_creation_records import CHOICE, EVENTS, HISTORY, CreationRecords, read_choice

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root creation")
REAL_COMMAND = CodexRuntime.command


def successor(
    case, monkeypatch, old, new, *, before=None, installed=None, database=None, account=None
):
    work = case[0][0][0]
    runtime = installed or case[0][3]
    invoke(
        monkeypatch,
        database or work[0],
        new,
        "create-main-successor",
        "--owner",
        work[1],
        "--session",
        work[-1]["id"],
        "--timeout",
        "35",
        "--cli",
        str(runtime.cli),
        "--tool-host",
        str(runtime.tool_host),
        "--previous-home",
        str(old),
        "--supersede",
        before or read_choice(old)["id"],
        "--authorize",
        account=account,
    )


def retire(case, monkeypatch, **options):
    old = prepare(case, monkeypatch, **options)
    observe(monkeypatch)
    abandon(case, monkeypatch, **options)
    return old


def snapshot(home):
    return {
        path.relative_to(home): path.read_bytes()
        for path in [home / CHOICE, *(home / EVENTS).iterdir()]
    }


def test_one_authorized_successor_keeps_original_bytes_and_same_business_session(
    creation_case,
    monkeypatch,
    private_runtime_directory,
    capsys,
):
    case = creation_case
    database, owner, _, _, _, _, session = case[0][0][0]
    old = retire(case, monkeypatch)
    capsys.readouterr()
    previous = read_choice(old)
    before = snapshot(old)
    with database.connect() as con:
        original = dict(attempt_rows(con, session["id"])[0][0])
    case[3][0] = "normal"
    new = private_runtime_directory("milo-successor-")
    successor(case, monkeypatch, old, new)
    result = json.loads(capsys.readouterr().out)
    assert result["association_recorded"] and result["persistence_verified"]
    assert result["session_id"] == session["id"]
    assert snapshot(old) == before
    assert snapshot(new / HISTORY / previous["id"]) == before
    with database.connect() as con:
        attempts, retirements, grants = attempt_rows(con, session["id"])
        assert len(attempts) == 2 and len(retirements) == len(grants) == 1
        assert dict(next(row for row in attempts if row["id"] == previous["id"])) == original
        assert not execution_busy(con)
    verify_history(new, attempts, retirements, grants)
    journal = PrivateJournal(new, initialize=False)
    assert journal.business_binding().owner_id == owner
    assert journal.business_binding().session_id == session["id"]
    methods = [item["method"] for item in requests(case)]
    assert methods.count("thread/start") == 2
    assert methods.count("thread/inject_items") == methods.count("thread/resume") == 1
    assert not any(method.startswith("turn/") for method in methods)
    unused = private_runtime_directory("milo-successor-duplicate-")
    with pytest.raises(SystemExit):
        successor(case, monkeypatch, old, unused)
    with pytest.raises(SystemExit):
        call(case, monkeypatch, recover=True)
    assert not list(unused.iterdir())
    assert [item["method"] for item in requests(case)] == methods


@pytest.mark.parametrize("change", ["wrong-id", "event-bytes", "original-config", "workspace"])
def test_successor_rejects_wrong_predecessor_before_publishing(
    creation_case,
    monkeypatch,
    private_runtime_directory,
    tmp_path,
    change,
):
    old = retire(creation_case, monkeypatch)
    before = read_choice(old)["id"]
    if change == "wrong-id":
        before = str(uuid4())
    elif change == "event-bytes":
        event = old / EVENTS / "000004.json"
        event.write_bytes(event.read_bytes() + b" ")
    elif change == "original-config":
        config = old / "config.toml"
        config.write_bytes(config.read_bytes() + b'\nextra = "unexpected"\n')
    else:
        work = creation_case[0][0][0]
        different = tmp_path / "different-workspace"
        different.mkdir()
        work[4].register_workspace(work[3], "Different workspace", different)
    new = private_runtime_directory("milo-successor-refused-")
    wire = requests(creation_case)
    with pytest.raises(SystemExit):
        successor(creation_case, monkeypatch, old, new, before=before)
    assert not list(new.iterdir()) and requests(creation_case) == wire


def test_failed_successor_claim_keeps_grant_and_receipt_out_of_database(
    creation_case,
    monkeypatch,
    private_runtime_directory,
):
    old = retire(creation_case, monkeypatch)
    database, *_, session = creation_case[0][0][0]
    new = private_runtime_directory("milo-successor-failed-")
    with database.connect(write=True) as con:
        con.execute(
            "CREATE TRIGGER fail_successor BEFORE INSERT ON work_main_creations "
            "WHEN NEW.supersedes_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
    wire = requests(creation_case)
    with pytest.raises(SystemExit):
        successor(creation_case, monkeypatch, old, new)
    assert requests(creation_case) == wire
    with database.connect() as con:
        attempts, receipts, grants = attempt_rows(con, session["id"])
        assert len(attempts) == 1 and not receipts and not grants and not execution_busy(con)
    assert (new / CHOICE).exists() and not CreationRecords(new).events


@pytest.mark.parametrize("regrant", [False, True])
def test_lost_successor_resume_can_only_recover_that_same_attempt(
    creation_case,
    monkeypatch,
    private_runtime_directory,
    regrant,
):
    old = retire(creation_case, monkeypatch)
    new = private_runtime_directory("milo-successor-lost-resume-")
    creation_case[3][0] = "resume-eof"
    with pytest.raises(SystemExit):
        successor(creation_case, monkeypatch, old, new)
    another = private_runtime_directory("milo-successor-after-loss-")
    count = len(requests(creation_case))
    with pytest.raises(SystemExit):
        successor(creation_case, monkeypatch, old, another)
    assert not list(another.iterdir()) and len(requests(creation_case)) == count
    creation_case[3][0] = "normal"
    new_case = ((creation_case[0][0], new, *creation_case[0][2:]), *creation_case[1:])
    if regrant:
        work = creation_case[0][0][0]
        work[4].revoke(work[1])
        work[4].grant(work[1])
        with pytest.raises(SystemExit):
            call(new_case, monkeypatch, recover=True)
        assert len(requests(creation_case)) == count
        return
    call(new_case, monkeypatch, recover=True)
    methods = [item["method"] for item in requests(creation_case)[count:]]
    assert methods == ["initialize", "initialized", "thread/resume"]
    assert PrivateJournal(new, initialize=False).business_binding()


@pytest.mark.skipif(
    not Path("/run/systemd/system").exists() or not TOOL_HOST.is_file(),
    reason="Requires local Codex and root systemd",
)
def test_native_successor_on_service_account_db_stops_and_reads_back_same_id(
    creation_case,
    monkeypatch,
    private_runtime_directory,
    owned_database,
    tmp_path,
):
    work = creation_case[0][0][0]
    repository = tmp_path / "git"
    subprocess.run(
        ["git", "init", "--quiet", "--template=", str(repository)], check=True, timeout=5
    )
    child = repository / "work"
    child.mkdir()
    local_config = repository / ".codex" / "config.toml"
    local_config.parent.mkdir()
    local_config.write_text('model = "untrusted-project-model"\n', encoding="utf-8")
    work[4].register_workspace(work[3], "Test isolated worktree", child)
    path, account = owned_database
    copied = path.with_name("seed.sqlite3")
    work[0].backup(copied)
    os.chown(copied, account.pw_uid, account.pw_gid)
    os.replace(copied, path)
    database = ServiceDatabase(path, account.pw_name)
    options = {"database": database, "account": account.pw_name}
    old = retire(creation_case, monkeypatch, **options)
    before = snapshot(old)
    new = private_runtime_directory("milo-real-successor-")
    profile = NewawzConfig._validated(
        {},
        "synthetic-provider",
        "synthetic-model",
        {
            "name": "Synthetic initialization only",
            "base_url": "https://synthetic.invalid",
            "env_key": "SYNTHETIC_KEY",
            "wire_api": "responses",
            "requires_openai_auth": False,
        },
    )
    monkeypatch.setattr(NewawzConfig, "load", lambda: profile)
    monkeypatch.setattr(CodexRuntime, "verify", VERIFY_INSTALLED)
    monkeypatch.setattr(CodexRuntime, "command", REAL_COMMAND)
    monkeypatch.setattr("milo_home.work_creation_process.DirectUnit", DirectUnit)
    successor(
        creation_case, monkeypatch, old, new, installed=CodexRuntime(CLI, TOOL_HOST), **options
    )
    records = CreationRecords(new)
    methods = [e["request"]["method"] for e in records.events if e["kind"] == "intent"]
    assert methods == [
        "initialize",
        "thread/start",
        "thread/inject_items",
        "initialize",
        "thread/resume",
    ]
    stops = [e["connection"] for e in records.events if e.get("phase") == "stop_confirmed"]
    assert len(stops) == len(set(stops)) == 2
    journal = PrivateJournal(new, initialize=False)
    assert journal.binding().model == "synthetic-model" and journal.binding().cwd == child
    assert journal.business_binding().session_id == work[-1]["id"]
    assert snapshot(old) == before and not list(new.glob("*.env"))
    assert (new / "config.toml").read_bytes() == profile.content
    with database.connect() as con:
        assert not execution_busy(con)
        assert con.execute("SELECT COUNT(*) FROM work_runtime_bindings").fetchone()[0] == 1

import json
import os
import sqlite3
import sys
from uuid import uuid4

import pytest
from test_cli_entrypoints import command
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_reconciliation import abandon
from test_work_reconciliation import fresh_stop as fresh_stop
from test_work_runtime import journal_at
from test_work_tasks import work_case as work_case

from milo_home.cli import main
from milo_home.codex_journal import PrivateJournal
from milo_home.database_identity import ServiceDatabase
from milo_home.execution_lock import execution_lock
from milo_home.work_runtime import WorkRuntime

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root CLI")


@pytest.fixture
def runtime_case(execution_case, tmp_path, private_runtime_directory):
    # Exercise real ancestor validation outside the project and public /tmp.
    directory = private_runtime_directory("milo-work-cli-")
    journal = journal_at(directory / "session", tmp_path)
    runtime = WorkRuntime(execution_case[0][0], journal, lambda: execution_case[-1][0])
    return execution_case, runtime, journal


def invoke(monkeypatch, runtime, owner, action, *arguments, account=None):
    options = ["--run-directory", str(runtime.journal.path.parent)]
    if account:
        options += ["--database-user", account]
    monkeypatch.setenv("MILO_DATABASE", str(runtime.database.path))
    monkeypatch.setattr(
        "milo_home.work_cli.WorkRuntime",
        lambda database, journal: WorkRuntime(database, journal, runtime.clock),
    )
    monkeypatch.setattr(
        sys, "argv", ["milo-home", *options, "work", action, "--owner", owner, *arguments]
    )
    main()


def test_real_status_stop_and_key_conflict_have_no_private_journal(
    runtime_case, monkeypatch, capsys
):
    case, runtime, journal = runtime_case
    task, window = reserve(case)
    journal.path.unlink()
    invoke(monkeypatch, runtime, case[0][1], "status", "--task", task["id"])
    report = json.loads(capsys.readouterr().out)
    assert report["task_status"] == "queued" and report["windows"][0]["id"] == window["id"]
    key = str(uuid4())
    for _ in range(2):
        invoke(monkeypatch, runtime, case[0][1], "stop", "--task", task["id"], "--key", key)
        assert json.loads(capsys.readouterr().out) == {
            "task_status": "stopping",
            "stop_requested": True,
        }
    other, _ = case[0][5].create(case[0][1], case[0][-1]["id"], "合成第二件", str(uuid4()))
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, runtime, case[0][1], "stop", "--task", other["id"], "--key", key)
    assert exit_.value.code == 1
    assert "请求标识已用于另一项工作停止" in capsys.readouterr().err
    assert not journal.path.exists()
    with case[0][0].connect() as connection:
        assert connection.execute("SELECT state FROM execution_windows").fetchone()[0] == "stopping"
        assert connection.execute("SELECT count(*) FROM work_stops").fetchone()[0] == 1


def test_inspect_replay_and_settle_enter_through_cli_without_rerunning(
    launch_case, fake_units, monkeypatch, fresh_stop, capsys
):
    case, runtime, journal, _, output, _ = launch_case
    _, window = abandon(launch_case, monkeypatch)
    before = output.read_bytes()
    journal_before = journal.path.read_bytes()
    invoke(monkeypatch, runtime, case[0][1], "inspect", "--window", window["id"])
    report = json.loads(capsys.readouterr().out)
    assert report["requires_reconciliation"] and report["publication_pending"]
    assert report["known_turns"] == 1 and not fresh_stop
    assert journal.path.read_bytes() == journal_before
    for _ in range(2):
        invoke(monkeypatch, runtime, case[0][1], "replay-results", "--window", window["id"])
        assert json.loads(capsys.readouterr().out) == {"known_turns": 1}
    for _ in range(2):
        invoke(
            monkeypatch, runtime, case[0][1], "settle", "--window", window["id"], "--confirm-stop"
        )
        assert json.loads(capsys.readouterr().out) == {
            "closed": True,
            "outcome": "completed",
            "blocked_by": [],
        }
    assert len(fresh_stop) == 1 and output.read_bytes() == before and len(fake_units[1]) == 1


def test_settle_explicit_repair_and_unresolved_exit_code(
    launch_case, fake_units, monkeypatch, fresh_stop, capsys
):
    case, runtime, _, _, output, _ = launch_case
    _, window = abandon(launch_case, monkeypatch, mode="missing")
    before = output.read_bytes()
    with pytest.raises(SystemExit) as exit_:
        invoke(
            monkeypatch, runtime, case[0][1], "settle", "--window", window["id"], "--confirm-stop"
        )
    assert exit_.value.code == 1
    assert json.loads(capsys.readouterr().out)["blocked_by"] == ["missing_receipts"]
    invoke(
        monkeypatch,
        runtime,
        case[0][1],
        "settle",
        "--window",
        window["id"],
        "--confirm-stop",
        "--repair-receipts",
    )
    assert json.loads(capsys.readouterr().out)["closed"]
    assert len(fresh_stop) == 2 and output.read_bytes() == before
    with runtime.database.connect() as connection:
        assert connection.execute("SELECT output FROM work_results").fetchone()[0] is None


@pytest.mark.parametrize("action", ["status", "stop", "inspect", "replay-results", "settle"])
@pytest.mark.parametrize("access", ["other", "revoked"])
def test_wrong_owner_and_revocation_never_stop_or_publish(
    launch_case, fake_units, monkeypatch, fresh_stop, capsys, action, access
):
    case, runtime, _, _, _, _ = launch_case
    task, window = abandon(launch_case, monkeypatch)
    owner = case[0][2] if access == "other" else case[0][1]
    if access == "revoked":
        case[0][4].revoke(owner)
    field, value = (
        ("--task", task["id"]) if action in {"status", "stop"} else ("--window", window["id"])
    )
    extra = ["--key", str(uuid4())] if action == "stop" else []
    if action == "settle":
        extra = ["--confirm-stop"]
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, runtime, owner, action, field, value, *extra)
    assert exit_.value.code == 1
    assert not fresh_stop and not capsys.readouterr().out
    with runtime.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM work_results").fetchone()[0] == 0


def test_service_account_inspection_restores_root_for_private_files(
    runtime_case, owned_database, monkeypatch, capsys
):
    case, runtime, journal = runtime_case
    _, window = reserve(case)
    path, account = owned_database
    copied = path.parent / "seeded.sqlite3"
    runtime.database.backup(copied)
    os.chown(copied, account.pw_uid, account.pw_gid)
    os.replace(copied, path)
    runtime = WorkRuntime(ServiceDatabase(path, account.pw_name), journal, runtime.clock)
    with execution_lock(runtime.database) as lease:
        runtime.bind(case[0][1], case[0][-1]["id"], lease=lease)
        runtime.claim_start(case[0][1], window["id"], lease=lease)
    invoke(
        monkeypatch,
        runtime,
        case[0][1],
        "inspect",
        "--window",
        window["id"],
        account=account.pw_name,
    )
    assert json.loads(capsys.readouterr().out)["recorded_stop"] == "missing"
    assert os.geteuid() == 0
    assert all(p.stat().st_uid == account.pw_uid for p in path.parent.iterdir())
    assert all(p.stat().st_uid == 0 for p in journal.path.parent.iterdir())


@pytest.mark.parametrize(
    "target", ["business-missing", "business-old", "journal-missing", "journal-old"]
)
def test_real_commands_do_not_create_or_migrate(runtime_case, target):
    case, runtime, journal = runtime_case
    _, window = reserve(case)
    if target == "business-missing":
        database = runtime.database.path.with_name("missing.sqlite3")
    else:
        database = runtime.database.path
    if target == "business-old":
        with runtime.database.connect(write=True) as connection:
            connection.execute("PRAGMA user_version=10")
    if target == "journal-missing":
        journal.path.unlink()
    if target == "journal-old":
        with journal._connect() as connection:
            connection.execute("PRAGMA user_version=1")
    existing = {p: p.read_bytes() for p in [database, journal.path] if p.exists()}
    result = command(
        database,
        "--run-directory",
        str(journal.path.parent),
        "work",
        "inspect",
        "--owner",
        case[0][1],
        "--window",
        window["id"],
    )
    assert result.returncode == 1 and result.stderr == "Configuration or file check failed.\n"
    assert not result.stdout
    assert all(p.read_bytes() == content for p, content in existing.items())
    assert database.exists() == (target != "business-missing")
    assert journal.path.exists() == (target != "journal-missing")


def test_help_and_required_stop_confirmation_do_not_open_data(tmp_path):
    path = tmp_path / "missing.sqlite3"
    result = command(path, "work", "--help", extra_env={"MILO_COOKIE_SECURE": "bad"})
    assert result.returncode == 0 and "replay-results" in result.stdout
    result = command(path, "work", "settle", "--owner", str(uuid4()), "--window", str(uuid4()))
    assert result.returncode == 2 and "--confirm-stop" in result.stderr
    assert not path.exists()


def test_existing_journal_read_only_rejects_mutation_and_keeps_version(runtime_case):
    _, _, journal = runtime_case
    reader = PrivateJournal(journal.path.parent, initialize=False, read_only=True)
    assert reader.binding() == journal.binding()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.open_stream("milo-work-" + "a" * 32 + ".service")


@pytest.mark.parametrize("directory", [None, "/tmp", "protected"])
def test_runtime_directory_is_explicit_private_and_external(tmp_path, directory):
    path = tmp_path / "missing.sqlite3"
    environment = {}
    if directory == "protected":
        root = tmp_path / "personal"
        root.mkdir(mode=0o700)
        directory = str(root)
        environment["MILO_PROTECTED_ROOTS"] = str(root)
    options = [] if directory is None else ["--run-directory", directory]
    result = command(
        path,
        *options,
        "work",
        "status",
        "--owner",
        str(uuid4()),
        "--task",
        str(uuid4()),
        extra_env=environment,
    )
    assert result.returncode == 1 and result.stderr == "Configuration or file check failed.\n"
    assert not path.exists() and not result.stdout


def test_identity_errors_are_sanitized_without_opening_new_records(runtime_case):
    case, runtime, journal = runtime_case
    _, window = reserve(case)
    runtime.database.path.chmod(0o644)
    result = command(
        runtime.database.path,
        "--run-directory",
        str(journal.path.parent),
        "work",
        "inspect",
        "--owner",
        case[0][1],
        "--window",
        window["id"],
    )
    assert result.returncode == 1 and result.stderr == "Configuration or file check failed.\n"
    assert not result.stdout and not journal.outstanding()


def test_multithreaded_management_refuses_before_database_access(monkeypatch, tmp_path):
    from milo_home.database import Database
    from milo_home.work_cli import require_management

    path = tmp_path / "missing.sqlite3"
    monkeypatch.setattr("milo_home.work_cli.threading.active_count", lambda: 2)
    with pytest.raises(ValueError, match="single-threaded"):
        require_management(Database(path), tmp_path)
    assert not path.exists()

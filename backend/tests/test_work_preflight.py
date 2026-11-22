import json
import os
import sys

import pytest
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_work_cli import runtime_case as runtime_case
from test_work_execution import counts
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.cli import main
from milo_home.codex_config import CodexRuntime, NewawzConfig
from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.database_identity import ServiceDatabase
from milo_home.execution_lock import execution_lock
from milo_home.scheduler_cli import StopRequest
from milo_home.work_credentials import load_credential
from milo_home.work_preflight import check_receiver
from milo_home.work_runtime import WorkRuntime

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root preflight")


def invoke(monkeypatch, launch_case, *, action="check", credential=None):
    case, runtime, journal, launcher, _, _ = launch_case
    monkeypatch.setenv("MILO_DATABASE", str(runtime.database.path))
    monkeypatch.setenv("SYNTHETIC_KEY", "synthetic-environment-fallback")
    monkeypatch.setattr("milo_home.work_execution.NewawzConfig.load", lambda: launcher.config)
    monkeypatch.setattr(
        "milo_home.work_execution.WorkRuntime",
        lambda database, journal: WorkRuntime(database, journal, runtime.clock),
    )
    argv = [
        "milo-home",
        "--run-directory",
        str(journal.path.parent),
        "work",
        action,
        "--owner",
        case[0][1],
        "--session",
        case[0][-1]["id"],
        "--timeout",
        "60",
    ]
    if credential is not None:
        argv += ["--credential-file", str(credential)]
    if action == "listen":
        argv.append("--execute")
    monkeypatch.setattr(sys, "argv", argv)
    main()


def snapshot(launch_case):
    _, runtime, journal, _, _, _ = launch_case
    return {
        path: path.read_bytes()
        for path in [runtime.database.path, *journal.path.parent.iterdir()]
        if path.is_file()
    }


def key_file(launch_case, content=b"synthetic-file-secret\n"):
    path = launch_case[2].path.parent / "provider.key"
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_cli_check_is_read_only_and_does_not_take_task_or_grant_execution(
    launch_case, fake_units, monkeypatch, capsys
):
    task = create(launch_case[0][0])
    key = key_file(launch_case)
    monkeypatch.setattr(CodexRuntime, "verify", lambda _: None)
    before = snapshot(launch_case)
    invoke(monkeypatch, launch_case, credential=key)
    assert json.loads(capsys.readouterr().out) == {
        "ready": True,
        "execution_available": False,
        "requested_timeout": 60,
        "allowed_timeout": 60,
    }
    assert snapshot(launch_case) == before
    assert counts(launch_case[1].database) == [0, 0, 0]
    assert launch_case[0][0][5].get(launch_case[0][0][1], task["id"])["status"] == "queued"
    assert not fake_units[1] and not launch_case[4].exists()


@pytest.mark.parametrize(
    "blocker", ["config", "digest", "binding", "private", "policy", "schedule", "version", "busy"]
)
def test_preflight_rejects_without_writing_or_spawning(
    launch_case, fake_units, monkeypatch, capsys, blocker
):
    case, runtime, journal, _, output, _ = launch_case
    monkeypatch.setattr(CodexRuntime, "verify", lambda _: None)
    if blocker == "config":
        (journal.path.parent / "config.toml").write_bytes(b"changed")
    elif blocker == "digest":
        with journal._connect() as connection:
            data = json.loads(connection.execute("SELECT value FROM binding").fetchone()[0])
            data["config_digest"] = "changed"
            connection.execute("UPDATE binding SET value=?", (json.dumps(data),))
    elif blocker == "binding":
        with journal._connect() as connection:
            connection.execute("DELETE FROM business_binding")
    elif blocker == "private":
        journal.open_stream("milo-work-" + "f" * 32 + ".service")
    elif blocker == "policy":
        with runtime.database.connect(write=True) as connection:
            connection.execute("UPDATE execution_policy SET policy=NULL")
    elif blocker == "schedule":
        case[2].pause(True)
    elif blocker == "busy":
        case[1].reserve(case[0][1], create(case[0])["id"], 60)
    else:

        def reject(_):
            raise ValueError("synthetic version failure")

        monkeypatch.setattr(CodexRuntime, "verify", reject)
    before = snapshot(launch_case)
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, launch_case)
    assert exit_.value.code == 1 and not capsys.readouterr().out
    assert snapshot(launch_case) == before
    assert not output.exists() and not fake_units[1]


def test_read_only_bind_cannot_repair_missing_main(launch_case):
    _, _, journal, launcher, _, _ = launch_case
    binding = journal.binding()
    with journal._connect() as connection:
        connection.execute("DELETE FROM binding")
    before = journal.path.read_bytes()
    readonly = PrivateJournal(journal.path.parent, initialize=False, read_only=True)
    with pytest.raises(ProtocolError):
        readonly.bind(binding, launcher.config, launcher.installed)
    assert journal.path.read_bytes() == before


@pytest.mark.parametrize("change", ["regrant", "stop"])
def test_preparation_rechecks_authority_and_stop(launch_case, monkeypatch, capsys, change):
    case = launch_case[0][0]

    def alter(_):
        if change == "regrant":
            case[4].revoke(case[1])
            case[4].grant(case[1])
        else:
            import signal

            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(CodexRuntime, "verify", alter)
    with pytest.raises(SystemExit):
        invoke(monkeypatch, launch_case)
    assert not capsys.readouterr().out and counts(launch_case[1].database) == [0, 0, 0]


def test_preflight_lock_conflict_never_retries(launch_case, monkeypatch, capsys):
    with execution_lock(launch_case[1].database), pytest.raises(SystemExit):
        invoke(monkeypatch, launch_case)
    assert "执行协调进程正在运行" in capsys.readouterr().err


@pytest.mark.parametrize("change", ["revoke", "config"])
def test_passed_preflight_does_not_authorize_later_listener(
    launch_case, fake_units, monkeypatch, capsys, change
):
    monkeypatch.setattr(CodexRuntime, "verify", lambda _: None)
    invoke(monkeypatch, launch_case)
    capsys.readouterr()
    case = launch_case[0][0]
    create(case)
    if change == "revoke":
        case[4].revoke(case[1])
    else:
        (launch_case[2].path.parent / "config.toml").write_bytes(b"changed")
    with pytest.raises(SystemExit):
        invoke(monkeypatch, launch_case, action="listen")
    assert not capsys.readouterr().out and not fake_units[1]
    assert counts(launch_case[1].database) == [0, 0, 0]


@pytest.mark.parametrize(
    "bad",
    ["missing", "public", "symlink", "fifo", "long", "multiline", "crlf", "nul", "utf8", "empty"],
)
def test_explicit_bad_credential_never_falls_back_or_leaks(
    launch_case, fake_units, monkeypatch, capsys, bad
):
    path = key_file(launch_case)
    if bad == "missing":
        path.unlink()
    elif bad == "public":
        path.chmod(0o644)
    elif bad == "symlink":
        target = path.with_suffix(".original")
        path.rename(target)
        path.symlink_to(target)
    elif bad == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(
            {
                "long": b"x" * 8194,
                "multiline": b"secret\n\n",
                "crlf": b"secret\r\n",
                "nul": b"secret\x00",
                "utf8": b"\xff",
                "empty": b"\n",
            }[bad]
        )
    with pytest.raises(SystemExit):
        invoke(monkeypatch, launch_case, credential=path)
    captured = capsys.readouterr()
    assert captured.err == "Configuration or file check failed.\n" and not captured.out
    assert counts(launch_case[1].database) == [0, 0, 0] and not fake_units[1]


def test_file_credential_preserves_whitespace_without_mutating_environment(
    launch_case, monkeypatch
):
    path = key_file(launch_case, b'  synthetic $secret " \\ value  \n')
    config = launch_case[3].config
    monkeypatch.setenv(config.env_key, "unchanged")
    assert load_credential(config, path) == path.read_text()[:-1]
    assert os.environ[config.env_key] == "unchanged"
    assert load_credential(config) == "unchanged"


def test_cli_listener_accepts_private_file_and_stops_after_one_task(
    launch_case, fake_units, monkeypatch, capsys
):
    case = launch_case[0][0]
    first, second = create(case), create(case)
    key = key_file(launch_case)
    invoke(monkeypatch, launch_case, action="listen", credential=key)
    assert json.loads(capsys.readouterr().out)["task_id"] == first["id"]
    assert case[5].get(case[1], second["id"])["status"] == "queued"
    assert counts(launch_case[1].database) == [1, 1, 1]
    assert key.exists() and not list(key.parent.glob("*.env"))
    assert len(fake_units[1]) == 1 and fake_units[1][0].process.poll() is not None


@pytest.mark.parametrize("schema", ["business", "private"])
def test_preflight_rejects_old_schema_without_migration(launch_case, monkeypatch, schema):
    runtime, journal = launch_case[1:3]
    if schema == "business":
        with runtime.database.connect(write=True) as connection:
            connection.execute("PRAGMA user_version=17")
    else:
        with journal._connect() as connection:
            connection.execute("PRAGMA user_version=1")
    before = snapshot(launch_case)
    with pytest.raises(SystemExit):
        invoke(monkeypatch, launch_case)
    assert snapshot(launch_case) == before


def test_service_owned_database_preflight_never_reads_root_files_as_service_uid(
    runtime_case, owned_database, monkeypatch
):
    case, runtime, journal = runtime_case
    path, account = owned_database
    copied = path.parent / "seeded.sqlite3"
    runtime.database.backup(copied)
    os.chown(copied, account.pw_uid, account.pw_gid)
    os.replace(copied, path)
    runtime.database = ServiceDatabase(path, account.pw_name)
    with execution_lock(runtime.database) as lease:
        runtime.bind(case[0][1], case[0][-1]["id"], lease=lease)
    readonly = PrivateJournal(journal.path.parent, initialize=False, read_only=True)
    checked = WorkRuntime(runtime.database, readonly, runtime.clock)
    config = NewawzConfig(
        "synthetic-provider",
        "synthetic-model",
        "SYNTHETIC_KEY",
        (journal.path.parent / "config.toml").read_bytes(),
    )
    monkeypatch.setattr(CodexRuntime, "verify", lambda _: None)
    before = path.read_bytes(), journal.path.read_bytes()
    report = check_receiver(
        checked,
        config,
        readonly.installation(),
        case[0][1],
        case[0][-1]["id"],
        60,
        "synthetic-only",
        StopRequest(),
    )
    assert report["ready"] and os.geteuid() == 0
    assert (path.read_bytes(), journal.path.read_bytes()) == before
    assert all(entry.stat().st_uid == account.pw_uid for entry in path.parent.iterdir())
    assert all(entry.stat().st_uid == 0 for entry in journal.path.parent.iterdir())

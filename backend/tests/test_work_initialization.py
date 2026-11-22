import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_runtime import SYNTHETIC_CONFIG
from test_work_setup import invoke
from test_work_tasks import work_case as work_case

from milo_home.codex_app_server import ThreadBinding
from milo_home.codex_config import CodexRuntime, NewawzConfig, private_create
from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.errors import ApiError
from milo_home.execution_lock import execution_lock
from milo_home.work_initialization import INTENT, initialize_main

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root registration")


@pytest.fixture
def init_case(execution_case, monkeypatch, private_runtime_directory):
    directory = private_runtime_directory("milo-main-init-")
    config = NewawzConfig(
        "synthetic-provider", "synthetic-model", "SYNTHETIC_KEY", SYNTHETIC_CONFIG
    )
    installed = CodexRuntime(Path("/synthetic/codex"), Path("/synthetic/host"))
    monkeypatch.setattr("milo_home.work_initialization.NewawzConfig.load", lambda: config)
    monkeypatch.setattr(CodexRuntime, "verify", lambda self: "codex-cli 0.167.0")
    return execution_case, directory, str(uuid4()), installed, config


def initialize(case, **overrides):
    execution, directory, thread, installed, _ = case
    work = execution[0]
    arguments = dict(
        database=work[0],
        directory=directory,
        owner=work[1],
        session_id=work[-1]["id"],
        thread_id=thread,
        installed=installed,
    )
    return initialize_main(**(arguments | overrides))


def cli_arguments(case):
    execution, _, thread, installed, _ = case
    return [
        "init-main",
        "--owner",
        execution[0][1],
        "--session",
        execution[0][-1]["id"],
        "--thread",
        thread,
        "--cli",
        str(installed.cli),
        "--tool-host",
        str(installed.tool_host),
    ]


def test_cli_initializes_once_without_credentials_protocol_or_execution(
    init_case, monkeypatch, capsys
):
    execution, directory, thread, installed, config = init_case
    database = execution[0][0]
    monkeypatch.delenv(config.env_key, raising=False)
    invoke(monkeypatch, database, directory, *cli_arguments(init_case))
    assert json.loads(capsys.readouterr().out) == {
        "session_id": execution[0][-1]["id"],
        "private_main_registered": True,
        "association_recorded": True,
        "protocol_verified": False,
        "execution_available": False,
    }
    journal = PrivateJournal(directory, initialize=False)
    assert journal.binding().thread_id == thread and journal.installation() == installed
    assert journal.business_binding().session_id == execution[0][-1]["id"]
    assert (directory / "config.toml").read_bytes() == config.content
    assert not journal.outstanding()
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    invoke(monkeypatch, database, directory, *cli_arguments(init_case))
    capsys.readouterr()
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert all(
        path.stat().st_uid == 0 and path.stat().st_mode & 0o077 == 0 for path in directory.iterdir()
    )
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM execution_windows").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM execution_start_intents").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["owner", "session", "version", "config", "busy", "lock"])
def test_preflight_failure_never_creates_private_files(init_case, monkeypatch, failure):
    execution, directory, _, _, _ = init_case
    kwargs = {}
    if failure == "owner":
        kwargs["owner"] = execution[0][2]
    elif failure == "session":
        kwargs["session_id"] = str(uuid4())
    elif failure == "version":

        def fail(_):
            raise ValueError("synthetic version mismatch")

        monkeypatch.setattr(CodexRuntime, "verify", fail)
    elif failure == "config":
        private_create(directory / "config.toml", b"unrelated configuration")
    elif failure == "busy":
        reserve(execution)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    if failure == "lock":
        with execution_lock(execution[0][0]), pytest.raises(ApiError, match="worker_busy"):
            initialize(init_case)
    else:
        with pytest.raises((ProtocolError, ValueError, ApiError)):
            initialize(init_case, **kwargs)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


@pytest.mark.parametrize("version", [0, 1])
def test_existing_old_journal_is_not_initialized_or_migrated(init_case, version):
    directory = init_case[1]
    path = directory / "session-journal.sqlite3"
    private_create(path, b"")
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={version}")
    before = path.read_bytes()
    with pytest.raises(ProtocolError, match="version"):
        initialize(init_case)
    assert path.read_bytes() == before
    assert set(p.name for p in directory.iterdir()) == {path.name}


@pytest.mark.parametrize("change", ["thread", "cli", "config", "database"])
def test_committed_selection_rejects_changed_retry(init_case, monkeypatch, change):
    import milo_home.work_initialization as module

    execution, directory, _, installed, config = init_case
    original = module._publish_file

    def interrupt(path, content):
        if path.name == "config.toml":
            raise OSError("synthetic interruption after committed choice")
        return original(path, content)

    monkeypatch.setattr(module, "_publish_file", interrupt)
    with pytest.raises(OSError):
        initialize(init_case)
    selected = (directory / INTENT).read_bytes()
    monkeypatch.setattr(module, "_publish_file", original)
    kwargs = {}
    if change == "thread":
        kwargs["thread_id"] = str(uuid4())
    elif change == "cli":
        kwargs["installed"] = replace(installed, cli=Path("/synthetic/other-codex"))
    elif change == "config":
        changed = replace(config, content=config.content + b"# changed\n")
        monkeypatch.setattr(module.NewawzConfig, "load", lambda: changed)
    else:
        database = execution[0][0]
        copy = database.path.with_name("changed-inode.sqlite3")
        database.backup(copy)
        os.replace(copy, database.path)
    with pytest.raises(ProtocolError, match="differs"):
        initialize(init_case, **kwargs)
    assert (directory / INTENT).read_bytes() == selected
    assert not (directory / "config.toml").exists()


@pytest.mark.parametrize("step", ["config", "journal", "private_binding", "business_half"])
def test_same_selection_finishes_partial_registration(init_case, monkeypatch, step):
    import milo_home.work_initialization as module

    target, name = {
        "config": (module, "_publish_file"),
        "journal": (module, "_publish_journal"),
        "private_binding": (PrivateJournal, "bind"),
        "business_half": (PrivateJournal, "bind_business"),
    }[step]
    original = getattr(target, name)

    def fail(*args, **kwargs):
        if step == "config" and args[0].name != "config.toml":
            return original(*args, **kwargs)
        raise OSError("synthetic partial registration")

    monkeypatch.setattr(target, name, fail)
    with pytest.raises(OSError):
        initialize(init_case)
    choice = (init_case[1] / INTENT).read_bytes()
    monkeypatch.setattr(target, name, original)
    assert initialize(init_case)["association_recorded"] is True
    journal = PrivateJournal(init_case[1], initialize=False)
    assert journal.binding().thread_id == init_case[2]
    assert journal.business_binding() is not None
    assert (init_case[1] / INTENT).read_bytes() == choice


@pytest.mark.parametrize("invalid_schema", [False, True])
def test_registration_ignores_only_legacy_version_after_upgrade(
    init_case, monkeypatch, invalid_schema
):
    import milo_home.work_initialization as module

    directory = init_case[1]
    publish = module._publish_file

    def interrupt_after_legacy_intent(path, content):
        if path.name == INTENT:
            legacy = json.loads(content)
            legacy["version"] = "codex-cli 0.162.1"
            if invalid_schema:
                legacy["schema"] = True
            publish(path, (json.dumps(legacy, sort_keys=True) + "\n").encode())
        else:
            raise OSError("interrupted before publishing config")

    monkeypatch.setattr(module, "_publish_file", interrupt_after_legacy_intent)
    with pytest.raises(OSError):
        initialize(init_case)
    original = (directory / INTENT).read_bytes()
    monkeypatch.setattr(module, "_publish_file", publish)
    if invalid_schema:
        with pytest.raises(ProtocolError, match="Original main registration"):
            initialize(init_case)
        assert not (directory / "session-journal.sqlite3").exists()
    else:
        assert initialize(init_case)["association_recorded"]
    assert (directory / INTENT).read_bytes() == original


@pytest.mark.parametrize("moment", ["version", "before_business"])
def test_one_invocation_cannot_cross_revocation_and_regrant(init_case, monkeypatch, moment):

    work = init_case[0][0]
    if moment == "version":

        def alter(_):
            work[4].revoke(work[1])
            work[4].grant(work[1])

        monkeypatch.setattr(CodexRuntime, "verify", alter)
    else:
        original = PrivateJournal.bind

        def bind_then_regrant(*args, **kwargs):
            original(*args, **kwargs)
            work[4].revoke(work[1])
            work[4].grant(work[1])

        monkeypatch.setattr(PrivateJournal, "bind", bind_then_regrant)
    with pytest.raises(ProtocolError, match="authorization"):
        initialize(init_case)
    with work[0].connect() as connection:
        assert connection.execute("SELECT count(*) FROM work_runtime_bindings").fetchone()[0] == 0
    if moment == "version":
        assert not list(init_case[1].iterdir())
    else:
        monkeypatch.setattr(PrivateJournal, "bind", original)
        assert initialize(init_case)["association_recorded"] is True


def test_unresolved_private_stream_refuses_registration_retry(init_case):
    initialize(init_case)
    journal = PrivateJournal(init_case[1], initialize=False)
    stream = journal.open_stream("milo-work-" + "b" * 32 + ".service")
    with pytest.raises(ProtocolError, match="reconciliation"):
        initialize(init_case)
    assert journal.outstanding()[0]["id"] == stream


def test_lost_associated_journal_is_not_recreated(init_case):
    initialize(init_case)
    path = init_case[1] / "session-journal.sqlite3"
    # Retain the original evidence under another explicit synthetic name.
    path.rename(path.with_name("preserved-journal.sqlite3"))
    with pytest.raises(ProtocolError, match="missing"):
        initialize(init_case)
    assert not path.exists()


def test_wrong_existing_main_does_not_pin_an_incorrect_new_intent(init_case):
    initialize(init_case)
    intent = init_case[1] / INTENT
    intent.rename(intent.with_name("preserved-intent.json"))
    with pytest.raises(ProtocolError, match="differs"):
        initialize(init_case, thread_id=str(uuid4()))
    assert not intent.exists()


@pytest.mark.parametrize("mismatch", ["fingerprint", "binding_id"])
def test_business_conflict_is_rejected_before_pinning_new_intent(init_case, mismatch):
    initialize(init_case)
    work = init_case[0][0]
    directory, thread, installed, config = init_case[1:]
    original = PrivateJournal(directory, initialize=False).business_binding()
    for name in [INTENT, "session-journal.sqlite3"]:
        path = directory / name
        path.rename(path.with_name("preserved-" + name))
    if mismatch == "fingerprint":
        thread = str(uuid4())
    journal = PrivateJournal(directory)
    journal.bind(
        ThreadBinding(thread, config.provider, config.model, work[0].path.parent), config, installed
    )
    if mismatch == "binding_id":
        journal.bind_business(replace(original, binding_id=str(uuid4())))
    with pytest.raises(ProtocolError, match="differs|another business"):
        initialize(init_case, thread_id=thread)
    assert not (directory / INTENT).exists()

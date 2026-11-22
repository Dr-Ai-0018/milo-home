import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_cli_entrypoints import command
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_cli import runtime_case as runtime_case
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.cli import main
from milo_home.codex_config import NewawzConfig
from milo_home.codex_journal import PrivateJournal
from milo_home.database import Database
from milo_home.database_identity import ServiceDatabase
from milo_home.execution_lock import execution_lock
from milo_home.work_access import WorkAccess

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root management")


def invoke(monkeypatch, database, directory, *arguments, account=None):
    options = ["--run-directory", str(directory)]
    if account:
        options += ["--database-user", account]
    monkeypatch.setenv("MILO_DATABASE", str(database.path))
    monkeypatch.setattr(sys, "argv", ["milo-home", *options, "work", "setup", *arguments])
    main()


@pytest.fixture
def setup_case(tmp_path, private_runtime_directory):
    directory = private_runtime_directory("milo-work-setup-")
    database = Database(tmp_path / "setup.sqlite3")
    database.initialize()
    owner, other, workspace = (str(uuid4()) for _ in range(3))
    with database.connect(write=True) as connection:
        for index, user in enumerate([owner, other]):
            connection.execute(
                "INSERT INTO users VALUES (?,?,?,'unused',1)",
                (user, f"setup{index}", "不是权限凭据"),
            )
    return database, directory, owner, other, workspace


def workspace_args(workspace, root, *, enabled=True):
    return [
        "workspace",
        "--workspace",
        workspace,
        "--name",
        "合成工作区",
        "--root",
        str(root),
        "--enable" if enabled else "--disable",
    ]


def patch_config(monkeypatch):
    config = NewawzConfig(
        "synthetic-provider",
        "synthetic-model",
        "SYNTHETIC_KEY",
        b'[model_providers.synthetic-provider]\nbase_url="https://synthetic.invalid"\n',
    )
    monkeypatch.setattr("milo_home.work_setup.NewawzConfig.load", lambda: config)
    monkeypatch.setattr("milo_home.codex_config.CodexRuntime.verify", lambda self: None)
    return config


def test_public_registration_show_and_explicit_capability(setup_case, monkeypatch, capsys):
    database, directory, owner, _, workspace = setup_case
    root = database.path.parent
    assert WorkAccess(database).workspaces(owner) == []
    for _ in range(2):
        invoke(monkeypatch, database, directory, *workspace_args(workspace, root))
        capsys.readouterr()
        invoke(monkeypatch, database, directory, "grant", "--owner", owner)
        capsys.readouterr()
    invoke(monkeypatch, database, directory, "show")
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "workspaces": [{"id": workspace, "name": "合成工作区", "enabled": 1, "revision": 1}],
        "operators": [{"user_id": owner, "enabled": 1, "revision": 1}],
        "sessions": [],
        "policy": None,
        "execution_available": False,
    }
    assert WorkAccess(database).workspaces(owner) == [{"id": workspace, "name": "合成工作区"}]
    assert not (directory / "session-journal.sqlite3").exists()
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM execution_windows").fetchone()[0] == 0


def test_grant_cannot_guess_unknown_user_or_replace_operator(setup_case, monkeypatch, capsys):
    database, directory, owner, other, _ = setup_case
    for user in [str(uuid4()), owner, other]:
        if user == owner:
            invoke(monkeypatch, database, directory, "grant", "--owner", user)
            capsys.readouterr()
        else:
            with pytest.raises(SystemExit) as exit_:
                invoke(monkeypatch, database, directory, "grant", "--owner", user)
            assert exit_.value.code == 1 and not capsys.readouterr().out
    with database.connect() as connection:
        assert connection.execute("SELECT user_id FROM work_access").fetchone()[0] == owner


def test_revoke_during_held_lease_does_not_release_window(runtime_case, monkeypatch, capsys):
    case, runtime, journal = runtime_case
    work = case[0]
    task, window = reserve(case)
    with execution_lock(runtime.database):
        invoke(monkeypatch, runtime.database, journal.path.parent, "revoke", "--owner", work[1])
    assert json.loads(capsys.readouterr().out) == {"owner_id": work[1], "enabled": False}
    invoke(monkeypatch, runtime.database, journal.path.parent, "grant", "--owner", work[1])
    capsys.readouterr()
    assert work[5].list(work[1], work[-1]["id"], 0, 50)["tasks"] == []
    with runtime.database.connect() as connection:
        row = connection.execute("SELECT * FROM work_tasks WHERE id=?", (task["id"],)).fetchone()
        assert row["status"] == "stopping" and row["stop_requested_at"] is not None
        assert (
            connection.execute(
                "SELECT state FROM execution_windows WHERE id=?", (window["id"],)
            ).fetchone()[0]
            == "stopping"
        )


def test_disable_workspace_cancels_queued_and_reenable_does_not_revive(
    runtime_case, monkeypatch, capsys
):
    case, runtime, journal = runtime_case
    work = case[0]
    task = create(work)
    for enabled in [False, True]:
        invoke(
            monkeypatch,
            runtime.database,
            journal.path.parent,
            *workspace_args(work[3], runtime.database.path.parent, enabled=enabled),
        )
        capsys.readouterr()
    assert work[5].list(work[1], work[-1]["id"], 0, 50)["tasks"] == []
    with runtime.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT status FROM work_tasks WHERE id=?", (task["id"],)
            ).fetchone()[0]
            == "cancelled"
        )
        assert connection.execute("SELECT revision FROM workspaces").fetchone()[0] == 3


@pytest.mark.parametrize("root_kind", ["relative", "linked", "missing"])
def test_workspace_refuses_noncanonical_roots(setup_case, monkeypatch, capsys, root_kind):
    database, directory, _, _, workspace = setup_case
    root = database.path.parent / "absent"
    if root_kind == "relative":
        root = Path(".")
    elif root_kind == "linked":
        root.symlink_to(database.path.parent)
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, database, directory, *workspace_args(workspace, root))
    assert exit_.value.code == 1 and not capsys.readouterr().out
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 0


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b'{"daily_launches":0,"daily_seconds":1}',
        b'{"daily_launches":true,"daily_seconds":null}',
        b'{"daily_launches":1,"daily_seconds":1,"extra":"private-value"}',
        b"not-json",
    ],
)
def test_policy_requires_explicit_valid_values_and_sanitizes_errors(
    setup_case, monkeypatch, capsys, payload
):
    database, directory, _, _, _ = setup_case
    policy = directory / "policy.json"
    policy.write_bytes(payload)
    policy.chmod(0o600)
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, database, directory, "policy", "--file", str(policy))
    captured = capsys.readouterr()
    assert exit_.value.code == 1 and not captured.out
    assert captured.err == "Configuration or file check failed.\n"
    with database.connect() as connection:
        assert connection.execute("SELECT policy FROM execution_policy").fetchone()[0] is None


def test_policy_explicit_null_and_show_do_not_read_private_runtime(setup_case, monkeypatch, capsys):
    database, directory, _, _, _ = setup_case
    policy = directory / "policy.json"
    policy.write_text('{"daily_launches":3,"daily_seconds":null}\n')
    policy.chmod(0o600)
    invoke(monkeypatch, database, directory, "policy", "--file", str(policy))
    assert json.loads(capsys.readouterr().out)["policy"] == {
        "daily_launches": 3,
        "daily_seconds": None,
    }
    before = database.path.read_bytes()
    invoke(monkeypatch, database, directory, "show")
    output = capsys.readouterr().out
    assert json.loads(output)["policy"]["daily_launches"] == 3
    assert database.path.read_bytes() == before and str(directory) not in output


def test_bind_same_main_retries_partial_private_commit(runtime_case, monkeypatch, capsys):
    case, runtime, journal = runtime_case
    work = case[0]
    patch_config(monkeypatch)
    original = PrivateJournal.bind_business
    monkeypatch.setattr(
        PrivateJournal, "bind_business", lambda *args: (_ for _ in ()).throw(OSError())
    )
    arguments = ["bind-main", "--owner", work[1], "--session", work[-1]["id"]]
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, runtime.database, journal.path.parent, *arguments)
    assert exit_.value.code == 1 and not capsys.readouterr().out
    assert journal.business_binding() is None
    with runtime.database.connect() as connection:
        original_id = connection.execute("SELECT binding_id FROM work_runtime_bindings").fetchone()[
            0
        ]
    monkeypatch.setattr(PrivateJournal, "bind_business", original)
    for _ in range(2):
        invoke(monkeypatch, runtime.database, journal.path.parent, *arguments)
        assert json.loads(capsys.readouterr().out) == {
            "session_id": work[-1]["id"],
            "association_recorded": True,
        }
    assert journal.business_binding().binding_id == original_id
    assert not journal.outstanding()


@pytest.mark.parametrize("failure", ["owner", "session", "config", "stream", "lock", "version"])
def test_bind_preflight_never_creates_business_association(
    runtime_case, monkeypatch, capsys, failure
):
    case, runtime, journal = runtime_case
    work = case[0]
    config = patch_config(monkeypatch)
    owner, session = work[1], work[-1]["id"]
    if failure == "owner":
        owner = work[2]
    elif failure == "session":
        session = str(uuid4())
    elif failure == "config":
        monkeypatch.setattr(
            "milo_home.work_setup.NewawzConfig.load", lambda: replace(config, model="changed")
        )
    elif failure == "stream":
        journal.open_stream("milo-work-" + "a" * 32 + ".service")
    elif failure == "version":

        def refuse(self):
            raise ValueError("synthetic-version-error")

        monkeypatch.setattr("milo_home.codex_config.CodexRuntime.verify", refuse)
    arguments = ["bind-main", "--owner", owner, "--session", session]
    if failure == "lock":
        with execution_lock(runtime.database), pytest.raises(SystemExit):
            invoke(monkeypatch, runtime.database, journal.path.parent, *arguments)
    else:
        with pytest.raises(SystemExit):
            invoke(monkeypatch, runtime.database, journal.path.parent, *arguments)
    assert not capsys.readouterr().out
    with runtime.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM work_runtime_bindings").fetchone()[0] == 0
    assert journal.business_binding() is None


def test_service_account_bind_keeps_private_access_under_root(
    runtime_case, owned_database, monkeypatch, capsys
):
    case, runtime, journal = runtime_case
    path, account = owned_database
    runtime.database.backup(path.parent / "seed.sqlite3")
    seed = path.parent / "seed.sqlite3"
    os.chown(seed, account.pw_uid, account.pw_gid)
    os.replace(seed, path)
    database = ServiceDatabase(path, account.pw_name)
    patch_config(monkeypatch)
    invoke(
        monkeypatch,
        database,
        journal.path.parent,
        "bind-main",
        "--owner",
        case[0][1],
        "--session",
        case[0][-1]["id"],
        account=account.pw_name,
    )
    assert json.loads(capsys.readouterr().out)["association_recorded"]
    assert journal.business_binding().database == str(path)
    assert os.geteuid() == 0
    assert all(p.stat().st_uid == 0 for p in journal.path.parent.iterdir())
    assert all(p.stat().st_uid == account.pw_uid for p in path.parent.iterdir())


@pytest.mark.parametrize("target", ["missing", "old"])
def test_setup_does_not_create_or_migrate_database(setup_case, target):
    database, directory, _, _, _ = setup_case
    if target == "missing":
        path = database.path.with_name("missing.sqlite3")
    else:
        path = database.path
        with database.connect(write=True) as connection:
            connection.execute("PRAGMA user_version=10")
    before = path.read_bytes() if path.exists() else None
    result = command(path, "--run-directory", str(directory), "work", "setup", "show")
    assert result.returncode == 1 and not result.stdout
    assert (path.read_bytes() if path.exists() else None) == before


def test_setup_help_and_required_enable_do_not_open_data(tmp_path):
    path = tmp_path / "missing.sqlite3"
    result = command(path, "work", "setup", "--help", extra_env={"MILO_COOKIE_SECURE": "bad"})
    assert result.returncode == 0 and "bind-main" in result.stdout
    result = command(path, "work", "setup", *workspace_args(str(uuid4()), tmp_path)[:-1])
    assert result.returncode == 2 and not path.exists()


@pytest.mark.parametrize("failure", ["public", "linked"])
def test_policy_refuses_unsafe_files_without_saving(setup_case, monkeypatch, capsys, failure):
    database, directory, _, _, _ = setup_case
    policy = directory / "policy.json"
    policy.write_text('{"daily_launches":1,"daily_seconds":10}\n')
    policy.chmod(0o600)
    selected = policy
    if failure == "public":
        policy.chmod(0o644)
    else:
        selected = directory / "linked.json"
        selected.symlink_to(policy)
    with pytest.raises(SystemExit):
        invoke(monkeypatch, database, directory, "policy", "--file", str(selected))
    assert not capsys.readouterr().out
    with database.connect() as connection:
        assert connection.execute("SELECT policy FROM execution_policy").fetchone()[0] is None


def test_setup_never_creates_missing_private_journal(runtime_case, monkeypatch, capsys):
    case, runtime, journal = runtime_case
    journal.path.unlink()
    with pytest.raises(SystemExit):
        invoke(
            monkeypatch,
            runtime.database,
            journal.path.parent,
            "bind-main",
            "--owner",
            case[0][1],
            "--session",
            case[0][-1]["id"],
        )
    assert not capsys.readouterr().out and not journal.path.exists()
    with runtime.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM work_runtime_bindings").fetchone()[0] == 0


def test_workspace_id_replacement_is_refused_without_touching_existing(
    runtime_case, monkeypatch, capsys
):
    _, runtime, journal = runtime_case
    with runtime.database.connect() as connection:
        original = dict(connection.execute("SELECT * FROM workspaces").fetchone())
    with pytest.raises(SystemExit):
        invoke(
            monkeypatch,
            runtime.database,
            journal.path.parent,
            *workspace_args(str(uuid4()), runtime.database.path.parent),
        )
    assert not capsys.readouterr().out
    with runtime.database.connect() as connection:
        assert dict(connection.execute("SELECT * FROM workspaces").fetchone()) == original


def test_another_private_main_cannot_complete_original_partial_binding(
    runtime_case, monkeypatch, capsys, private_runtime_directory
):
    from test_work_runtime import journal_at

    case, runtime, journal = runtime_case
    work = case[0]
    patch_config(monkeypatch)
    invoke(
        monkeypatch,
        runtime.database,
        journal.path.parent,
        "bind-main",
        "--owner",
        work[1],
        "--session",
        work[-1]["id"],
    )
    capsys.readouterr()
    original = journal.business_binding()
    directory = private_runtime_directory("milo-other-setup-")
    other = journal_at(directory / "session", runtime.database.path.parent, thread="different-main")
    with pytest.raises(SystemExit):
        invoke(
            monkeypatch,
            runtime.database,
            other.path.parent,
            "bind-main",
            "--owner",
            work[1],
            "--session",
            work[-1]["id"],
        )
    assert not capsys.readouterr().out
    assert other.business_binding() is None and journal.business_binding() == original

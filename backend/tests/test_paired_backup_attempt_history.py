"""Pairing keeps the successful main and the earlier, explicitly retired evidence."""

import json
import os
import sqlite3
from contextlib import closing

import pytest
from test_execution_control import execution_case as execution_case
from test_work_creation import creation_case as creation_case
from test_work_creation_abandon import abandon, observe, prepare
from test_work_initialization import init_case as init_case
from test_work_process import fake_units as fake_units
from test_work_setup import invoke
from test_work_tasks import work_case as work_case
from test_workspace_backup import commit

from milo_home.codex_journal import PrivateJournal
from milo_home.database import Database
from milo_home.paired_backup import capture_generation, rehearse_generation
from milo_home.work_attempt_history import HISTORY
from milo_home.work_creation_records import CHOICE, EVENTS, read_choice
from milo_home.workspace_git import git

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Paired backup requires root")


def test_successor_backup_roundtrip_verifies_retirement_and_current_main(
    creation_case, monkeypatch, tmp_path, private_runtime_directory, capsys
):
    previous = prepare(creation_case, monkeypatch)
    observe(monkeypatch)
    abandon(creation_case, monkeypatch)
    capsys.readouterr()
    original_id = read_choice(previous)["id"]
    root = private_runtime_directory("milo-paired-successor-")
    home = root / "home"
    home.mkdir(mode=0o700)
    work = creation_case[0][0][0]
    database, owner = work[:2]
    session = work[-1]["id"]
    installed = creation_case[0][3]
    creation_case[3][0] = "normal"
    invoke(
        monkeypatch,
        database,
        home,
        "create-main-successor",
        "--owner",
        owner,
        "--session",
        session,
        "--timeout",
        "60",
        "--cli",
        str(installed.cli),
        "--tool-host",
        str(installed.tool_host),
        "--previous-home",
        str(previous),
        "--supersede",
        original_id,
        "--authorize",
    )
    assert json.loads(capsys.readouterr().out)["association_recorded"]
    thread = PrivateJournal(home, initialize=False, read_only=True).binding().thread_id
    (home / "sessions").mkdir(mode=0o700)
    (home / "sessions/rollout.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n"
    )
    with closing(sqlite3.connect(home / "state_5.sqlite")) as connection, connection:
        connection.execute("CREATE TABLE threads(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO threads VALUES (?)", (thread,))
    git(tmp_path, "init", "--template=", "--initial-branch=main")
    (tmp_path / ".gitignore").write_text("*.sqlite3*\ncreation-requests.ndjson\n")
    (tmp_path / "work.txt").write_text("initial workspace\n")
    git(tmp_path, "add", ".gitignore", "work.txt")
    commit(tmp_path, "Synthetic successor workspace")

    archive, output = root / "archive", root / "restored"
    captured = capture_generation(database, home, tmp_path, archive, writers_stopped=True)
    report = rehearse_generation(archive, output, captured["manifest_sha256"])
    assert report["file_restore_verified"] and not report["protocol_restore_verified"]
    manifest = json.loads((archive / "manifest.json").read_bytes())
    assert HISTORY in manifest["baseline"]["native"]["omitted"]
    assert not (output / "native" / HISTORY).exists()
    retired = output / "runtime" / HISTORY / original_id
    for path in [previous / CHOICE, *sorted((previous / EVENTS).iterdir())]:
        assert (retired / path.relative_to(previous)).read_bytes() == path.read_bytes()
    with Database(output / "business.sqlite3").connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM work_main_attempt_retirements").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM work_main_attempt_grants").fetchone()[0] == 1
        )
    assert (
        PrivateJournal(output / "runtime", initialize=False, read_only=True).binding().thread_id
        == thread
    )

    audit = sorted((home / HISTORY / original_id / EVENTS).iterdir())[-1]
    damaged = json.loads(audit.read_bytes())
    damaged["thread_start_delivery"] = "success"
    audit.write_text(json.dumps(damaged) + "\n")
    with pytest.raises(ValueError, match="creation history or current binding differs"):
        capture_generation(database, home, tmp_path, root / "bad", writers_stopped=True)
    assert not (root / "bad").exists()

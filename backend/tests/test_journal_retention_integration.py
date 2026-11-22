import json
import sqlite3
import sys
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_journal_retention_scope import successful_reply, successful_work
from test_native_reply_execution import native_reply_case as native_reply_case
from test_native_reply_execution import tick
from test_reply_execution import ready, send
from test_reply_readiness import preparation as preparation
from test_work_process import fake_units as fake_units
from test_work_process import launch_case
from test_work_runtime import journal_at
from test_work_tasks import work_case as work_case
from test_workspace_backup import commit

from milo_home.cli import main
from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.journal_retention import archive
from milo_home.native_messages import NativeMessageStore
from milo_home.native_policy import NativePolicyStore
from milo_home.native_reply_results import final_result
from milo_home.paired_backup import capture_generation, rehearse_generation
from milo_home.work_runtime import WorkRuntime
from milo_home.workspace_git import git


@pytest.fixture
def managed_work(execution_case, monkeypatch, tmp_path, private_runtime_directory):
    root = private_runtime_directory("milo-retention-entry-")
    journal = journal_at(root / "home", tmp_path)
    runtime = WorkRuntime(execution_case[0][0], journal, lambda: execution_case[-1][0])
    return launch_case.__wrapped__((execution_case, runtime, journal), monkeypatch, tmp_path)


def invoke(case, monkeypatch, action, *options):
    _, runtime, journal, *_ = case
    monkeypatch.setenv("MILO_DATABASE", str(runtime.database.path))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "milo-home",
            "--run-directory",
            str(journal.path.parent),
            "work",
            "storage",
            action,
            *options,
        ],
    )
    main()


def test_real_cli_inspects_archives_and_refuses_second_base_without_execution(
    managed_work, fake_units, monkeypatch, capsys
):
    successful_work(managed_work)
    _, _, journal, _, output, _ = managed_work
    stream = journal.list_streams()[0]["stream"]["id"]
    snapshot = journal.stream_snapshot(stream)
    requests = output.read_bytes()
    invoke(managed_work, monkeypatch, "inspect", "--stream", stream)
    report = json.loads(capsys.readouterr().out)
    assert report["admission"]["eligible"] and report["storage"] is None
    invoke(managed_work, monkeypatch, "archive", "--stream", stream, "--confirm")
    assert json.loads(capsys.readouterr().out)["archived"]
    assert journal.stream_snapshot(stream) == snapshot
    assert output.read_bytes() == requests and len(fake_units[1]) == 1
    with pytest.raises(SystemExit) as error:
        invoke(managed_work, monkeypatch, "archive", "--stream", stream, "--confirm")
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out)["blocked_by"] == ["already_archived"]
    assert output.read_bytes() == requests


def test_native_reply_remains_readable_and_next_batch_runs_after_archival(
    native_reply_case, client
):
    successful_reply(native_reply_case, client)
    case, journal, _, _, units = native_reply_case
    database, _, _, _, directory, *_ = case[0]
    stream = journal.list_streams()[0]["stream"]["id"]
    snapshot = journal.stream_snapshot(stream)
    with database.snapshot() as connection:
        run_id = connection.execute("SELECT id FROM runs").fetchone()[0]
    saved, events = NativeMessageStore(journal).inspect(run_id)
    accepted = next(event for event in events if event.kind == "accepted")
    result = final_result(journal, stream, saved, accepted.turn_id)
    runtime = WorkRuntime(database, journal, lambda: case[1][0])
    assert archive(runtime, stream, reply_directory=directory)["archived"]
    assert journal.stream_snapshot(stream) == snapshot
    assert final_result(journal, stream, saved, accepted.turn_id) == result
    send(client, case)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    assert len(units[1]) == 2 and journal.storage_summary()["archives"][0]["stream"] == stream
    latest_sequence = max(
        sequence
        for item in journal.list_streams()
        for sequence, _ in journal.stream_snapshot(item["stream"]["id"])["entries"]
    )
    assert latest_sequence > snapshot["entries"][-1][0]


@pytest.mark.parametrize("native_reply_case", [str(uuid4())], indirect=True)
def test_paired_cold_native_reply_restores_all_original_links_without_source_home(
    native_reply_case, client
):
    case, journal, requests, *_ = native_reply_case
    database, _, _, _, directory, *_ = case[0]
    home = journal.path.parent
    workspace = journal.binding().cwd
    # Only the native files needed for the offline fixture; no model is contacted.
    (home / "sessions").mkdir(mode=0o700)
    (home / "sessions/rollout.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": journal.binding().thread_id}}) + "\n"
    )
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE threads(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO threads VALUES (?)", (journal.binding().thread_id,))
    git(workspace, "init", "--template=", "--initial-branch=main")
    (workspace / "work.txt").write_text("Synthetic paired cold history\n")
    git(workspace, "add", "work.txt")
    commit(workspace, "Synthetic paired cold history")
    send(client, case, 2)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    stream = journal.list_streams()[0]["stream"]["id"]
    snapshot = journal.stream_snapshot(stream)
    runtime = WorkRuntime(database, journal, lambda: case[1][0])
    assert archive(runtime, stream, reply_directory=directory)["archived"]
    send(client, case)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    with database.snapshot() as connection:
        runs = [row[0] for row in connection.execute("SELECT id FROM runs ORDER BY id")]
        publications = [
            tuple(row)
            for row in connection.execute(
                "SELECT id,body,conversation_id,in_reply_to FROM messages "
                "WHERE role='milo' ORDER BY id"
            )
        ]
    saved = {run: NativeMessageStore(journal).inspect(run) for run in runs}
    before_requests = requests.read_bytes()
    storage = journal.storage_summary()
    policy = NativePolicyStore(journal).read()
    root = directory.parent / ("paired-segments-" + uuid4().hex)
    root.mkdir(mode=0o700)
    captured = capture_generation(
        database,
        home,
        workspace,
        root / "archive",
        writers_stopped=True,
        reply_directory=directory,
    )
    manifest = json.loads((root / "archive/manifest.json").read_bytes())
    assert manifest["version"] == 3
    observations = manifest["process_observations"]["after"]
    assert observations["version"] == 2 and observations["archived_count"] == 1
    home.rename(home.with_name("source-home-unavailable"))
    report = rehearse_generation(root / "archive", root / "restored", captured["manifest_sha256"])
    assert report["file_restore_verified"] and not report["automatic_resume"]
    assert not report["protocol_restore_verified"] and not report["remote_verified"]
    assert report["native_replies"]["routes_restored"] == 2
    copied = PrivateJournal(root / "restored/runtime", initialize=False, read_only=True)
    assert copied.storage_summary() == storage
    assert copied.stream_snapshot(stream) == snapshot
    with sqlite3.connect(root / "restored/runtime/native-session-policy.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT body FROM revisions ORDER BY revision DESC LIMIT 1"
            ).fetchone()[0]
            == policy.encoded()
        )
    with pytest.raises(ProtocolError, match="installation differs"):
        NativePolicyStore(copied).read()
    assert copied.binding_record()["codex_home"] == str(home)
    for run in runs:
        assert NativeMessageStore(copied).inspect(run) == saved[run]
        route = json.loads((root / "restored/replies" / run / "native-reply.json").read_bytes())
        assert route["home"] == str(home)
    with sqlite3.connect(root / "restored/business.sqlite3") as connection:
        restored = connection.execute(
            "SELECT id,body,conversation_id,in_reply_to FROM messages WHERE role='milo' ORDER BY id"
        ).fetchall()
    assert restored == publications and requests.read_bytes() == before_requests

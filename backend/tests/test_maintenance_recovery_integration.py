"""Preserved synthetic paired material for exact remote recovery in L5."""

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from test_native_messages import message_case as message_case
from test_native_policy import execution_case as execution_case
from test_native_policy import native_case as native_case
from test_native_policy import runtime_case as runtime_case
from test_native_policy import work_case as work_case
from test_native_reply_execution import native_reply_case as native_reply_case
from test_native_reply_execution import tick
from test_reply_execution import ready, send
from test_reply_readiness import preparation as preparation
from test_work_process import fake_units as fake_units
from test_workspace_backup import commit

from milo_home.codex_journal import PrivateJournal
from milo_home.database import Database
from milo_home.database_identity import private_run_directory
from milo_home.execution_lock import execution_lock
from milo_home.journal_retention import archive
from milo_home.maintenance_backup import preflight_backup, rehearse_for_maintenance
from milo_home.maintenance_policy import CANDIDATE_BUDGET, load_backup_policy
from milo_home.native_message_scope import NativeMessageScope, NativeRevisitContext
from milo_home.native_messages import NativeMessageStore
from milo_home.paired_backup import capture_generation
from milo_home.runtime_evidence_backup import capture_runtime, rehearse_runtime
from milo_home.work_attachment_batches import read_attachment, seal_attachments
from milo_home.work_attachment_store import AttachmentStore
from milo_home.work_runtime import WorkRuntime
from milo_home.work_tasks import WorkTasks
from milo_home.workspace_archive_files import json_bytes, write_file
from milo_home.workspace_git import git

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Private paired recovery needs root")


@pytest.mark.parametrize(
    "native_reply_case", ["11111111-1111-4111-8111-111111111111"], indirect=True
)
def test_synthetic_attachment_cold_hot_mapping_and_routes_restore(
    native_reply_case, client, private_runtime_directory
):
    """Local fake IPC only; retained outputs are synthetic, not production evidence."""
    case, journal, requests, scenario, units = native_reply_case
    database, owner, _, _, replies, *_ = case[0]
    home = journal.path.parent
    workspace = journal.binding().cwd
    runtime = WorkRuntime(database, journal, lambda: case[1][0])

    case[2].configure("reply", owner, "milo-home-reply.timer", enabled=True, revisits=True)
    scenario[0] = "auto-reply-future-supplement"
    send(client, case)
    ready(case)
    assert tick(native_reply_case)["outcome"] == "succeeded"
    first = journal.list_streams()[0]["stream"]["id"]
    original_cold = journal.stream_snapshot(first)
    assert archive(runtime, first, reply_directory=replies)["archived"]

    with database.snapshot() as connection:
        case[1][0] = connection.execute("SELECT due_at FROM revisit_intents").fetchone()[0]
    scenario[0] = "auto-reply-followup"
    assert tick(native_reply_case)["outcome"] == "succeeded"
    assert all(unit.process.poll() is not None for unit in units[1])
    assert len(journal.segment_catalogue()) == 1
    assert journal.stream_snapshot(first) == original_cold
    with journal._connect() as connection:
        hot_entries = connection.execute("SELECT count(*) FROM entries").fetchone()[0]
        assert hot_entries > 0
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    with database.snapshot() as connection:
        runs = [row[0] for row in connection.execute("SELECT id FROM runs ORDER BY rowid")]
        session = connection.execute("SELECT id FROM work_sessions").fetchone()[0]
        bodies = [row[0] for row in connection.execute("SELECT body FROM messages ORDER BY rowid")]
    before_mapping = {run: NativeMessageStore(journal).inspect(run) for run in runs}
    assert len(before_mapping) == 2 and all(value is not None for value in before_mapping.values())
    revisit_scopes = [value[0] for value in before_mapping.values() if value[0].revisit is not None]
    assert len(revisit_scopes) == 1 and revisit_scopes[0].message_ids == ()

    # The fixture's IPC peer does not persist Codex files. Explicit synthetic
    # metadata/history exercise file recovery, without claiming native protocol.
    (home / "sessions").mkdir(mode=0o700)
    rollout = [{"type": "session_meta", "payload": {"id": journal.binding().thread_id}}]
    rollout.extend(
        {"type": "event_msg", "payload": {"type": "synthetic_maintenance_history", "text": body}}
        for body in bodies
    )
    history = ("\n".join(json.dumps(item) for item in rollout) + "\n").encode()
    write_file(home / "sessions/rollout.jsonl", history)
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE threads(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO threads VALUES (?)", (journal.binding().thread_id,))

    git(workspace, "init", "--template=", "--initial-branch=main")
    (workspace / "work.txt").write_bytes(b"Synthetic maintenance baseline\n")
    git(workspace, "add", "work.txt")
    commit(workspace, "Synthetic maintenance recovery baseline")
    (workspace / "work.txt").write_bytes(b"Synthetic maintenance baseline\nStaged edit\n")
    git(workspace, "add", "work.txt")
    (workspace / "work.txt").write_bytes(
        b"Synthetic maintenance baseline\nStaged edit\nUnstaged edit\n"
    )
    (workspace / "new-result.md").write_bytes(b"# Synthetic new result\n")

    preserve = os.environ.get("MILO_PRESERVE_L5_SYNTHETIC") == "1"
    if preserve:
        parent = private_run_directory(Path(os.environ["MILO_PRESERVE_L5_DIRECTORY"]))
        probe = Path(tempfile.mkdtemp(prefix="longterm-l5-synthetic-", dir=parent))
    else:
        probe = private_runtime_directory("milo-maintenance-recovery-")
    task, _ = WorkTasks(database).create(owner, session, "合成恢复交付，不是真实交办", str(uuid4()))
    task_id = task["id"]
    with database.connect(write=True) as connection:
        connection.execute("UPDATE work_tasks SET status='completed' WHERE id=?", (task_id,))
    handoff = workspace / "tasks" / task_id / "handoff"
    handoff.mkdir(parents=True)
    attachment_bytes = b"\x00Synthetic L5 nonempty sealed attachment\xff\n"
    (handoff / "result.bin").write_bytes(attachment_bytes)
    sealed = probe / "sealed"
    sealed.mkdir(mode=0o700)
    attachment_store = AttachmentStore(sealed, max_bytes=512 * 1024**2)
    with execution_lock(database) as lease:
        item = seal_attachments(database, owner, task_id, store=attachment_store, lease=lease)[
            "attachments"
        ][0]
    selected = ["new-result.md", f"tasks/{task_id}/handoff/result.bin"]
    policy_path = probe / "policy.json"
    write_file(
        policy_path,
        json_bytes(
            {
                "version": 1,
                "budget_bytes": CANDIDATE_BUDGET,
                "include": selected,
                "exclude": [],
                "native_exclude": [],
            }
        ),
    )
    policy = load_backup_policy(policy_path)
    options = {"attachment_store": attachment_store, "reply_directory": replies}
    checked = preflight_backup(database, home, workspace, probe / "archive", policy, **options)
    assert checked["ready"]
    captured = capture_generation(
        database,
        home,
        workspace,
        probe / "archive",
        writers_stopped=True,
        **policy.paired_options(),
        **options,
    )
    digest = captured["manifest_sha256"]
    # Preserve the trust value separately, before rehearsal reads the archive.
    write_file(probe / "manifest.sha256", (digest + "\n").encode())
    assert hashlib.sha256((probe / "archive/manifest.json").read_bytes()).hexdigest() == digest
    report = rehearse_for_maintenance(probe / "archive", probe / "restored", digest, policy)
    assert report["file_restore_verified"] and report["attachments_restored"] == 1
    assert report["native_replies"]["routes_restored"] == 2
    assert report["automatic_resume"] is False and report["remote_verified"] is False
    restored = PrivateJournal(probe / "restored/runtime", initialize=False, read_only=True)
    assert restored.storage_summary() == journal.storage_summary()
    assert restored.stream_snapshot(first) == original_cold
    assert {run: NativeMessageStore(restored).inspect(run) for run in runs} == before_mapping
    copied = AttachmentStore(probe / "restored/attachments", max_bytes=512 * 1024**2)
    assert (
        read_attachment(
            Database(probe / "restored/business.sqlite3"), owner, task_id, item["id"], store=copied
        )[1]
        == attachment_bytes
    )
    assert (probe / "restored/workspace/work.txt").read_bytes() == (
        workspace / "work.txt"
    ).read_bytes()
    assert (probe / "restored/workspace/new-result.md").read_bytes() == (
        workspace / "new-result.md"
    ).read_bytes()
    assert (probe / "restored/native/sessions/rollout.jsonl").read_bytes() == (
        home / "sessions/rollout.jsonl"
    ).read_bytes()
    assert len([json.loads(line) for line in requests.read_bytes().splitlines()]) > 0
    proof = {
        "synthetic": True,
        "timestamp": int(datetime.now(UTC).timestamp()),
        "generation_id": captured["generation"],
        "manifest_sha256": digest,
        "budget_bytes": policy.budget_bytes,
        "policy_sha256": policy.sha256,
        "attachments_restored": 1,
        "attachment_bytes": len(attachment_bytes),
        "cold_segments": len(restored.segment_catalogue()),
        "hot_entries": hot_entries,
        "native_deliveries_verified": len(before_mapping),
        "revisit_deliveries_verified": len(revisit_scopes),
        "routes_restored": report["native_replies"]["routes_restored"],
        "file_restore_verified": True,
        "protocol_restore_verified": False,
        "remote_verified": False,
        "automatic_resume": False,
        "model_calls": 0,
        "native_history_source": "explicit synthetic fixture records; not Codex protocol evidence",
    }
    write_file(probe / "synthetic-proof.json", json_bytes(proof))
    if preserve:
        print("L5_PROBE=" + str(probe))
        print("L5_MANIFEST_SHA256=" + digest)


@pytest.mark.parametrize(
    "ledger,version,revisit,accepted",
    [
        ("message", 1, False, True),
        ("message", 2, False, True),
        ("message", 2, True, True),
        ("message", 3, False, False),
        ("policy", 2, False, False),
    ],
)
def test_runtime_evidence_versions_and_v2_revisit_restore(
    message_case, private_runtime_directory, ledger, version, revisit, accepted
):
    runtime, store, original = message_case
    with execution_lock(runtime.database) as lease:
        store.claim(original, lease=lease)
        if revisit:
            following = NativeMessageScope.model_validate(
                original.model_dump()
                | {
                    "run_id": uuid4(),
                    "message_ids": (),
                    "revisit": NativeRevisitContext(
                        id=uuid4(), revision=1, due_at=100, source_ids=original.message_ids
                    ),
                }
            )
            store.claim(following, lease=lease)
    source = (
        store.path if ledger == "message" else store.directory / "native-session-policy.sqlite3"
    )
    with sqlite3.connect(source) as connection:
        if ledger == "message" and version == 1:
            connection.execute("DROP TABLE revisits")
        connection.execute(f"PRAGMA user_version={version}")
    root = private_runtime_directory("milo-maintenance-ledger-versions-")
    if not accepted:
        with pytest.raises(ValueError, match="Unsupported native evidence schema"):
            capture_runtime(store.directory, root / "archive", writers_stopped=True)
        return
    captured = capture_runtime(store.directory, root / "archive", writers_stopped=True)
    report = rehearse_runtime(root / "archive", root / "restored", captured["manifest_sha256"])
    assert report["restore_verified"] and report["automatic_resume"] is False
    restored = PrivateJournal(root / "restored", initialize=False, read_only=True)
    assert NativeMessageStore(restored).inspect(original.run_id)[0] == original
    if revisit:
        assert NativeMessageStore(restored).inspect(following.run_id)[0] == following

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_work_runtime import journal_at
from test_work_tasks import work_case as work_case
from test_workspace_backup import commit

from milo_home.codex_journal import PrivateJournal
from milo_home.database import Database
from milo_home.execution_lock import execution_lock
from milo_home.messages import MessageService
from milo_home.native_messages import NativeMessageEvent, NativeMessageScope, NativeMessageStore
from milo_home.native_policy import NativePolicyStore
from milo_home.native_reply_input import native_input_digest, native_reply_input
from milo_home.native_reply_route import save_native_route
from milo_home.paired_backup import capture_generation, rehearse_generation
from milo_home.runs import RunStore
from milo_home.work_attachment_batches import read_attachment, seal_attachments
from milo_home.work_attachment_store import AttachmentStore
from milo_home.work_runtime import WorkRuntime
from milo_home.work_tasks import WorkTasks
from milo_home.workspace_git import git


@pytest.fixture
def paired_reply_case(execution_case, private_runtime_directory, request):
    work, _, _, now = execution_case
    database, owner, _, _, _, _, session = work
    with database.snapshot() as connection:
        workspace = Path(connection.execute("SELECT root_path FROM workspaces").fetchone()[0])
        thread = connection.execute("SELECT id FROM threads WHERE owner_id=?", (owner,)).fetchone()[
            0
        ]
    root = private_runtime_directory("milo-paired-native-replies-")
    home, replies = root / "home", root / "replies"
    replies.mkdir(mode=0o700)
    journal = journal_at(home, workspace, thread=str(uuid4()))
    runtime = WorkRuntime(database, journal, lambda: now[0])
    (home / "AGENTS.md").write_bytes(b"Synthetic registered rules\n")
    (home / "AGENTS.md").chmod(0o600)
    with execution_lock(database) as lease:
        runtime.bind(owner, session["id"], lease=lease)
        policy = NativePolicyStore(journal).configure(
            owner=owner, session=session["id"], model="gpt-6.2-sol", expected=None, now=now[0]
        )
    incoming, _ = MessageService(database).post(owner, thread, "合成备份用留言", str(uuid4()))
    store = RunStore(database, lambda: now[0])
    run_id = store.queue(thread, 60)
    with database.connect(write=True) as connection:
        connection.execute("INSERT INTO reply_run_scopes VALUES (?,?)", (run_id, owner))
    runtime = WorkRuntime(database, journal, lambda: now[0])
    save_native_route(replies / run_id, runtime, owner, run_id)
    stage = getattr(request, "param", "unknown")
    stream_id = None
    if stage != "before-stream":
        stream_id = journal.open_stream("milo-reply-" + run_id + ".service")
    if stage not in {"before-stream", "before-claim"}:
        prompt = native_reply_input(store.snapshot(run_id, include_history=False), owner)
        scope = NativeMessageScope(
            run_id=run_id,
            owner_id=owner,
            conversation_id=incoming["conversation_id"],
            business_binding_id=journal.business_binding().binding_id,
            baseline_fingerprint=journal.fingerprint(),
            policy_digest=policy.digest,
            native_thread_id=journal.binding().thread_id,
            message_ids=(incoming["id"],),
            input_digest=native_input_digest(prompt),
        )
        with execution_lock(database) as lease:
            links = NativeMessageStore(journal)
            links.claim(scope, lease=lease)
            links.record(
                scope,
                NativeMessageEvent(kind="sent", stream_id=stream_id, request_id=1),
                lease=lease,
            )
        if stage != "before-intent":
            journal.record(
                stream_id,
                {
                    "kind": "intent",
                    "request": {
                        "id": 1,
                        "method": "turn/start",
                        "params": {
                            "threadId": scope.native_thread_id,
                            "clientUserMessageId": scope.input_id,
                            "input": [{"type": "text", "text": prompt}],
                        },
                    },
                    "reply": scope.model_dump(mode="json"),
                },
            )
            journal.record(stream_id, {"kind": "unknown", "id": 1})
    store.stop(run_id, "interrupted")
    (home / "sessions").mkdir(mode=0o700)
    (home / "sessions/rollout.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": journal.binding().thread_id}}) + "\n"
    )
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO threads VALUES (?)", (journal.binding().thread_id,))
    git(workspace, "init", "--template=", "--initial-branch=main")
    (workspace / ".gitignore").write_text("*.sqlite3*\n")
    (workspace / "work.txt").write_text("Synthetic work artifact\n")
    git(workspace, "add", ".gitignore", "work.txt")
    commit(workspace, "Synthetic native backup baseline")
    return root, database, home, workspace, replies, journal, run_id, owner


def capture(case, **kwargs):
    root, database, home, workspace, replies, *_ = case
    return capture_generation(
        database,
        home,
        workspace,
        root / "archive",
        writers_stopped=True,
        reply_directory=replies,
        **kwargs,
    )


def test_same_generation_retains_native_unknown_and_restores_without_original_home(
    paired_reply_case,
):
    root, database, home, _, _, journal, run_id, _ = paired_reply_case
    before = NativeMessageStore(journal).inspect(run_id)
    captured = capture(paired_reply_case)
    manifest = json.loads((root / "archive/manifest.json").read_bytes())
    assert manifest["version"] == 4 and "replies" in manifest["components"]
    assert manifest["public_assets"] == {"source": None, "index": []}
    home.rename(root / "original-home-away")
    report = rehearse_generation(root / "archive", root / "restored", captured["manifest_sha256"])
    assert report["file_restore_verified"] and report["automatic_resume"] is False
    assert report["native_replies"] == {"routes_restored": 1, "automatic_resume": False}
    copied = PrivateJournal(root / "restored/runtime", initialize=False, read_only=True)
    assert NativeMessageStore(copied).inspect(run_id) == before
    assert copied.outstanding()[0]["delivery"] == "unknown"
    with Database(root / "restored/business.sqlite3").snapshot() as connection:
        assert (
            connection.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0]
            == "stopping"
        )
    route = json.loads((root / "restored/replies" / run_id / "native-reply.json").read_bytes())
    assert route["home"] == str(home) and database.path.exists()


@pytest.mark.parametrize(
    "paired_reply_case", ["before-stream", "before-claim", "before-intent"], indirect=True
)
def test_crash_gaps_are_preserved_without_inventing_later_facts(paired_reply_case):
    root, _, _, _, _, journal, run_id, _ = paired_reply_case
    before = NativeMessageStore(journal).inspect(run_id)
    captured = capture(paired_reply_case)
    report = rehearse_generation(root / "archive", root / "restored", captured["manifest_sha256"])
    restored = PrivateJournal(root / "restored/runtime", initialize=False, read_only=True)
    assert NativeMessageStore(restored).inspect(run_id) == before
    assert report["automatic_resume"] is False


@pytest.mark.parametrize("damage", ["missing", "owner", "turn"])
def test_native_route_or_turn_conflicts_block_whole_generation(paired_reply_case, damage):
    root, _, _, _, replies, journal, run_id, _ = paired_reply_case
    path = replies / run_id / "native-reply.json"
    if damage == "missing":
        path.unlink()
    elif damage == "owner":
        value = json.loads(path.read_bytes())
        value["owner"] = str(uuid4())
        path.write_text(json.dumps(value))
    else:
        with journal._connect() as db:
            db.execute(
                "UPDATE entries SET payload=json_set(payload,'$.reply.run_id',?) "
                "WHERE json_extract(payload,'$.kind')='intent'",
                (str(uuid4()),),
            )
    with pytest.raises(ValueError, match="route|intent"):
        capture(paired_reply_case)
    assert not (root / "archive").exists()


def test_native_reply_cannot_be_backed_up_as_legacy_without_route_component(paired_reply_case):
    root, database, home, workspace, *_ = paired_reply_case
    with pytest.raises(ValueError, match="recovery routes"):
        capture_generation(database, home, workspace, root / "archive", writers_stopped=True)
    assert not (root / "archive").exists()


@pytest.mark.parametrize("paired_reply_case", ["before-stream"], indirect=True)
@pytest.mark.parametrize("damage", ["owner", "unit"])
def test_even_before_launch_route_requires_original_owner_and_unit(paired_reply_case, damage):
    root, database, _, _, replies, _, run_id, owner = paired_reply_case
    with database.connect(write=True) as connection:
        if damage == "unit":
            connection.execute("UPDATE runs SET unit_name='unrelated' WHERE id=?", (run_id,))
        else:
            other = connection.execute("SELECT id FROM users WHERE id!=?", (owner,)).fetchone()[0]
            connection.execute("DROP TRIGGER reply_run_scope_immutable")
            connection.execute(
                "UPDATE reply_run_scopes SET owner_id=? WHERE run_id=?", (other, run_id)
            )
    if damage == "owner":
        path = replies / run_id / "native-reply.json"
        route = json.loads(path.read_bytes())
        route["owner"] = other
        path.write_text(json.dumps(route))
    with pytest.raises(ValueError, match="owner|execution family"):
        capture(paired_reply_case)
    assert not (root / "archive").exists()


@pytest.mark.parametrize("field", ["id", "params.threadId", "params.input[0].text"])
def test_conflicting_rpc_facts_do_not_match_only_by_annotation(paired_reply_case, field):
    root, _, _, _, _, journal, _, _ = paired_reply_case
    with journal._connect() as connection:
        connection.execute(
            "UPDATE entries SET payload=json_set(payload,?,?) "
            "WHERE json_extract(payload,'$.kind')='intent'",
            ("$.request." + field, 99 if field == "id" else "wrong-input"),
        )
    with pytest.raises(ValueError, match="request conflicts"):
        capture(paired_reply_case)
    assert not (root / "archive").exists()


def test_native_routes_and_attachments_share_one_generation(paired_reply_case):
    root, database, _, workspace, _, _, _, owner = paired_reply_case
    tasks = WorkTasks(database)
    with database.snapshot() as connection:
        session = connection.execute("SELECT id FROM work_sessions").fetchone()[0]
    task, _ = tasks.create(owner, session, "合成附件交付", str(uuid4()))
    with database.connect(write=True) as connection:
        connection.execute("UPDATE work_tasks SET status='completed' WHERE id=?", (task["id"],))
    handoff = workspace / "tasks" / task["id"] / "handoff"
    handoff.mkdir(parents=True)
    (handoff / "result.bin").write_bytes(b"native-paired-attachment")
    store_root = root / "sealed"
    store_root.mkdir(mode=0o700)
    store = AttachmentStore(store_root, max_bytes=4096)
    with execution_lock(database) as lease:
        item = seal_attachments(database, owner, task["id"], store=store, lease=lease)[
            "attachments"
        ][0]
    (handoff / "result.bin").unlink()
    captured = capture(paired_reply_case, attachment_store=store)
    report = rehearse_generation(root / "archive", root / "restored", captured["manifest_sha256"])
    assert report["attachments_restored"] == 1 and report["native_replies"]["routes_restored"] == 1
    copied = AttachmentStore(root / "restored/attachments", max_bytes=4096)
    assert (
        read_attachment(
            Database(root / "restored/business.sqlite3"),
            owner,
            task["id"],
            item["id"],
            store=copied,
        )[1]
        == b"native-paired-attachment"
    )


def test_explicit_cli_routes_capture_and_rehearsal(paired_reply_case, monkeypatch, capsys):
    from milo_home.cli import main

    root, database, home, workspace, replies, *_ = paired_reply_case
    monkeypatch.setenv("MILO_DATABASE", str(database.path))
    monkeypatch.setattr(
        "sys.argv",
        [
            "milo-home",
            "--run-directory",
            str(home),
            "backup-paired",
            "capture",
            "--workspace",
            str(workspace),
            "--reply-directory",
            str(replies),
            "--writers-stopped",
            "--out",
            str(root / "archive"),
        ],
    )
    main()
    captured = json.loads(capsys.readouterr().out)
    monkeypatch.setenv("MILO_DATABASE", "unused-for-offline-native-reply-rehearsal")
    monkeypatch.setattr(
        "sys.argv",
        [
            "milo-home",
            "backup-paired",
            "rehearse",
            "--archive",
            str(root / "archive"),
            "--sha256",
            captured["manifest_sha256"],
            "--out",
            str(root / "restored"),
        ],
    )
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["native_replies"]["routes_restored"] == 1
    assert report["automatic_resume"] is False

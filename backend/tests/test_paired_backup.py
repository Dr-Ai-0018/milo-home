"""A whole capture window and its original cross-store association must agree."""

import hashlib
import json
import os
import sqlite3
import subprocess
from uuid import uuid4

import pytest
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_runtime import bind, journal_at
from test_work_tasks import work_case as work_case
from test_workspace_backup import commit

from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.database import Database
from milo_home.database_identity import ServiceDatabase
from milo_home.execution_lock import execution_lock
from milo_home.paired_backup import capture_generation, rehearse_generation
from milo_home.paired_backup_state import business_snapshot
from milo_home.work_attachment_batches import read_attachment, seal_attachments
from milo_home.work_attachment_store import AttachmentStore
from milo_home.work_runtime import WorkRuntime
from milo_home.work_tasks import WorkTasks
from milo_home.workspace_git import git

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Paired backup requires root")


@pytest.fixture
def paired_case(execution_case, tmp_path, private_runtime_directory):
    return generation_case(execution_case, tmp_path, private_runtime_directory)


def generation_case(execution_case, tmp_path, private_runtime_directory):
    root = private_runtime_directory("milo-generation-")
    work, _, _, now = execution_case
    home = root / "home"
    thread = str(uuid4())
    journal = journal_at(home, tmp_path, thread=thread)
    runtime = WorkRuntime(work[0], journal, lambda: now[0])
    _, window = reserve(execution_case)
    with execution_lock(work[0]) as lease:
        bind((execution_case, runtime, journal), lease)
        intent = runtime.claim_start(work[1], window["id"], lease=lease)
    journal.record(
        intent.stream_id,
        {"kind": "intent", "request": {"id": 1, "method": "turn/start"}},
    )
    journal.record(intent.stream_id, {"kind": "unknown", "id": 1})
    (home / "sessions").mkdir(mode=0o700)
    (home / "sessions/rollout.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n"
    )
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE threads(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO threads VALUES (?)", (thread,))
    (home / "main-registration.json").write_text('{"synthetic":"original"}\n')
    (home / "main-registration.json").chmod(0o600)
    (home / "auth.json").write_text("SYNTHETIC_SECRET_MUST_BE_EXCLUDED")
    git(tmp_path, "init", "--template=", "--initial-branch=main")
    (tmp_path / ".gitignore").write_text("*.sqlite3*\n")
    (tmp_path / "work.txt").write_text("original\n")
    git(tmp_path, "add", ".gitignore", "work.txt")
    commit(tmp_path, "Synthetic paired baseline")
    (tmp_path / "work.txt").write_text("original\nstaged\n")
    git(tmp_path, "add", "work.txt")
    (tmp_path / "work.txt").write_text("original\nstaged\nunstaged\n")
    (tmp_path / "extra.txt").write_text("Synthetic untracked result\n")
    return root, work[0], home, tmp_path, journal, intent


def capture(case, **options):
    root, database, home, workspace, _, _ = case
    return capture_generation(
        database,
        home,
        workspace,
        root / "archive",
        writers_stopped=True,
        include=["extra.txt"],
        **options,
    )


def test_indexed_attachments_restore_with_the_same_generation(paired_case, monkeypatch, capsys):
    from milo_home.cli import main

    root, database, _, workspace, _, intent = paired_case
    with database.connect() as connection:
        active = connection.execute(
            "SELECT owner_id,session_id FROM work_tasks WHERE id=?", (intent.task_id,)
        ).fetchone()
    owner = active["owner_id"]
    tasks = WorkTasks(database)
    task, _ = tasks.create(owner, active["session_id"], "交回合成附件", str(uuid4()))
    task_id = task["id"]
    with database.connect(write=True) as connection:
        connection.execute("UPDATE work_tasks SET status='completed' WHERE id=?", (task_id,))
    handoff = workspace / "tasks" / task_id / "handoff"
    handoff.mkdir(parents=True)
    original = b"\x00paired-attachment"
    (handoff / "attachment.bin").write_bytes(original)
    store_root = root / "sealed"
    store_root.mkdir(mode=0o700)
    store = AttachmentStore(store_root, max_bytes=1024)
    with execution_lock(database) as lease:
        item = seal_attachments(database, owner, task_id, store=store, lease=lease)["attachments"][
            0
        ]
    (handoff / "attachment.bin").unlink()
    (handoff / "ignored").write_bytes(b"unsealed")
    unsealed = f"tasks/{task_id}/handoff/ignored"
    with pytest.raises(ValueError, match="requires the indexed attachment store"):
        capture(paired_case, exclude=[unsealed])
    archive = root / "archive"
    output = root / "restored"
    monkeypatch.setenv("MILO_DATABASE", str(database.path))
    monkeypatch.setenv("MILO_WORK_ATTACHMENT_ROOT", str(store_root))
    monkeypatch.setenv("MILO_WORK_ATTACHMENT_MAX_BYTES", "1024")
    monkeypatch.setattr(
        "sys.argv",
        [
            "milo-home",
            "--run-directory",
            str(paired_case[2]),
            "backup-paired",
            "capture",
            "--workspace",
            str(workspace),
            "--writers-stopped",
            "--include",
            "extra.txt",
            "--exclude",
            unsealed,
            "--out",
            str(archive),
        ],
    )
    main()
    captured = json.loads(capsys.readouterr().out)
    monkeypatch.setenv("MILO_DATABASE", "unusable-during-rehearsal")
    monkeypatch.setattr(
        "sys.argv",
        [
            "milo-home",
            "backup-paired",
            "rehearse",
            "--archive",
            str(archive),
            "--sha256",
            captured["manifest_sha256"],
            "--out",
            str(output),
        ],
    )
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["attachments_restored"] == 1
    restored = AttachmentStore(output / "attachments", max_bytes=1024)
    assert read_attachment(
        Database(output / "business.sqlite3"), owner, task_id, item["id"], store=restored
    ) == (item, original)
    archived_file = archive / "attachments" / "tasks" / task_id / item["id"]
    archived_file.write_bytes(b"changed")
    with pytest.raises(ValueError, match="total manifest"):
        rehearse_generation(archive, root / "rejected", captured["manifest_sha256"])


def test_one_generation_restores_all_four_materials_and_retains_unknown(paired_case):
    root, database, home, workspace, journal, intent = paired_case
    original = journal.work_snapshot(intent.stream_id)
    captured = capture(paired_case)
    report = rehearse_generation(root / "archive", root / "restored", captured["manifest_sha256"])
    assert report["generation"] == captured["generation"]
    assert report["file_restore_verified"] and not report["automatic_resume"]
    for key in (
        "quiescence_verified",
        "protocol_restore_verified",
        "execution_binding_verified",
        "remote_verified",
    ):
        assert report[key] is False
    assert report["business"]["active_work_windows_retained"] == 1
    restored = PrivateJournal(root / "restored/runtime", initialize=False, read_only=True)
    assert (
        restored.work_snapshot(intent.stream_id)
        == original
        == journal.work_snapshot(intent.stream_id)
    )
    assert restored.outstanding()[0]["delivery"] == "unknown"
    assert (root / "restored/native/sessions/rollout.jsonl").read_bytes() == (
        home / "sessions/rollout.jsonl"
    ).read_bytes()
    assert (root / "restored/workspace/work.txt").read_bytes() == (
        workspace / "work.txt"
    ).read_bytes()
    assert (root / "restored/workspace/extra.txt").read_bytes() == (
        workspace / "extra.txt"
    ).read_bytes()
    assert not (root / "restored/runtime/config.toml").exists()
    with pytest.raises(ProtocolError, match="installation differs"):
        restored.installation()
    copied = Database(root / "restored/business.sqlite3")
    linked = journal.business_binding()
    with (
        execution_lock(copied) as lease,
        pytest.raises(ProtocolError, match="Existing business main association"),
    ):
        WorkRuntime(copied, journal).check_session_binding(
            linked.owner_id, linked.session_id, lease=lease
        )
    assert not (root / "restored/INCOMPLETE").exists()
    assert database.path.exists()
    for path in (root / "archive").rglob("*"):
        if path.is_file():
            assert b"SYNTHETIC_SECRET_MUST_BE_EXCLUDED" not in path.read_bytes()


@pytest.mark.parametrize("schema", [18, 19, 20])
def test_historical_generation_remains_an_exact_offline_rehearsal(paired_case, monkeypatch, schema):
    import milo_home.database_inspection as inspection
    import milo_home.paired_backup as paired
    import milo_home.paired_backup_state as state

    database = paired_case[1]
    with database.connect(write=True) as connection:
        tables = [
            "work_attachments",
            "work_attachment_batches",
            "work_receipt_tasks",
            "work_steer_inputs",
            "work_steer_turns",
            "work_deliverables",
        ]
        if schema < 20:
            tables.append("work_artifacts")
        if schema < 19:
            tables += ["work_main_attempt_grants", "work_main_attempt_retirements"]
        for table in tables:
            connection.execute(f"DROP TABLE {table}")
        connection.execute(f"PRAGMA user_version={schema}")
    with monkeypatch.context() as old:
        old.setattr(paired, "CURRENT_SCHEMA_VERSION", schema)
        old.setattr(inspection, "CURRENT_SCHEMA_VERSION", schema)
        old.setattr(
            inspection,
            "RECORD_TABLES",
            tuple(name for name in inspection.RECORD_TABLES if name not in tables),
        )
        old.setattr(state, "CURRENT_SCHEMA_VERSION", schema)
        captured = capture(paired_case)
    root = paired_case[0]
    report = rehearse_generation(root / "archive", root / "restored", captured["manifest_sha256"])
    assert report["file_restore_verified"]
    assert (
        report["business"]["copied_schema_version"]
        == report["business"]["schema_version"]
        == schema
    )


def test_native_auxiliary_files_still_require_exact_exclusions(paired_case):
    root, _, home, _, _, _ = paired_case
    for name in ("shell_snapshots", "skills/.system", "thread-writer-locks", "tmp"):
        (home / name).mkdir(mode=0o700, parents=True)
    (home / "skills/.system/generated.md").write_text("Synthetic built-in skill\n")
    (home / "thread-writer-locks/.coordination.lock").touch()
    with pytest.raises(ValueError, match="Unclassified native state"):
        capture(paired_case, native_exclude=["skills/.system"])
    assert not (root / "archive").exists()

    captured = capture(
        paired_case, native_exclude=["skills/.system", "thread-writer-locks/.coordination.lock"]
    )
    report = rehearse_generation(root / "archive", root / "restored", captured["manifest_sha256"])
    assert report["file_restore_verified"]
    assert (root / "restored/native/state_5.sqlite").is_file()
    assert (root / "restored/native/sessions/rollout.jsonl").is_file()
    assert not (root / "restored/native/skills").exists()
    assert not (root / "restored/native/thread-writer-locks").exists()
    assert report["business"]["active_work_windows_retained"] == 1


def test_only_an_empty_native_sqlite_maintenance_lock_is_omitted(paired_case):
    root, _, home, _, _, _ = paired_case
    lock = home / ".sqlite-maintenance.lock"
    lock.write_bytes(b"unexpected content")
    with pytest.raises(ValueError, match="Unexpected native SQLite maintenance lock"):
        capture(paired_case)
    lock.write_bytes(b"")
    captured = capture(paired_case)
    report = rehearse_generation(root / "archive", root / "restored", captured["manifest_sha256"])
    assert report["file_restore_verified"]
    assert (root / "restored/native/state_5.sqlite").is_file()
    assert not (root / "restored/native" / lock.name).exists()
    assert not (root / "restored/runtime" / lock.name).exists()


@pytest.mark.parametrize("source", ["business", "runtime", "native"])
def test_other_source_changes_while_git_is_captured_fail_the_whole_generation(
    paired_case, monkeypatch, source
):
    import milo_home.paired_backup_components as module

    root, database, home, _, _, _ = paired_case
    original = module.capture_workspace

    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        if source == "business":
            with database.connect(write=True) as connection:
                connection.execute("UPDATE users SET display_name='Synthetic changed'")
        elif source == "runtime":
            (home / "main-registration.json").write_text('{"synthetic":"changed"}\n')
        else:
            (home / "sessions/rollout.jsonl").write_text("changed\n")
        return result

    monkeypatch.setattr(module, "capture_workspace", mutate)
    with pytest.raises(ValueError, match="changed across"):
        capture(paired_case)
    assert (root / "archive/INCOMPLETE").exists()
    assert not (root / "archive/manifest.json").exists()


def test_other_workspace_or_database_cannot_join_the_generation(paired_case):
    root, database, home, workspace, _, _ = paired_case
    other = root / "other"
    other.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="association"):
        capture_generation(database, home, other, root / "archive", writers_stopped=True)
    database.backup(other / "other.sqlite3")
    with pytest.raises(ValueError, match="identity differs"):
        capture_generation(
            Database(other / "other.sqlite3"),
            home,
            workspace,
            root / "archive",
            writers_stopped=True,
        )
    assert not (root / "archive").exists()


@pytest.mark.parametrize(
    "artifact",
    [
        "business.sqlite3",
        "runtime/manifest.json",
        "native/state/sessions/rollout.jsonl",
        "workspace/staged.patch",
        "manifest.json",
    ],
)
def test_any_changed_component_is_rejected_before_restore(paired_case, artifact):
    root, _, _, _, _, _ = paired_case
    captured = capture(paired_case)
    path = root / "archive" / artifact
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="differ"):
        rehearse_generation(root / "archive", root / "restored", captured["manifest_sha256"])
    assert not (root / "restored").exists()


@pytest.mark.parametrize("field", ["thread_id", "workspace_sha256"])
def test_trusted_total_manifest_cannot_claim_a_different_thread_or_git_state(paired_case, field):
    root, _, _, _, _, _ = paired_case
    capture(paired_case)
    path = root / "archive/manifest.json"
    value = json.loads(path.read_bytes())
    value["baseline"][field] = str(uuid4()) if field == "thread_id" else "0" * 64
    content = json.dumps(value).encode()
    path.write_bytes(content)
    with pytest.raises(ValueError, match="sources differ"):
        rehearse_generation(
            root / "archive", root / "restored", hashlib.sha256(content).hexdigest()
        )
    assert not (root / "restored").exists()


def test_stop_capacity_and_nested_output_gates(paired_case):
    root, database, home, workspace, _, _ = paired_case
    with pytest.raises(ValueError, match="stopped-writer"):
        capture_generation(database, home, workspace, root / "archive")
    with pytest.raises(ValueError, match="budget"):
        capture(paired_case, budget=1)
    with pytest.raises(ValueError, match="disjoint"):
        capture_generation(database, home, workspace, home / "archive", writers_stopped=True)
    assert not (root / "archive").exists()


def test_service_account_snapshot_preserves_wal_and_does_not_access_private_output_as_owner(
    owned_database, private_runtime_directory
):
    path, account = owned_database
    directory_identity = (path.parent.stat().st_dev, path.parent.stat().st_ino)
    root = private_runtime_directory("milo-generation-owner-")
    database = ServiceDatabase(path, account.pw_name)
    try:
        script = (
            "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute(\"INSERT INTO auth_buckets VALUES ('synthetic-paired-wal',1,1)\"); "
            "c.commit(); print('READY',flush=True); sys.stdin.readline(); c.close()"
        )
        # The independent service-account writer holds a real committed WAL
        # open; the root coordinator never enters its UID while touching output.
        with subprocess.Popen(
            ["/usr/bin/python3", "-c", script, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            user=account.pw_uid,
            group=account.pw_gid,
        ) as writer:
            assert writer.stdout.readline().strip() == "READY"
            assert path.with_name(path.name + "-wal").stat().st_size > 0
            business_snapshot(database, root / "copy.sqlite3", 64 * 1024 * 1024)
            writer.stdin.close()
            assert writer.wait(timeout=3) == 0
        assert os.geteuid() == 0
        assert (root / "copy.sqlite3").stat().st_uid == 0
        assert all(p.stat().st_uid == account.pw_uid for p in path.parent.iterdir())
        with Database(root / "copy.sqlite3").connect() as connection:
            assert (
                connection.execute("SELECT key_hash FROM auth_buckets").fetchone()[0]
                == "synthetic-paired-wal"
            )
        with pytest.raises(ValueError, match="budget"):
            business_snapshot(database, root / "failed.sqlite3", 1)
        assert os.geteuid() == 0
    finally:
        # This legacy fixture deliberately retains its synthetic source. Here
        # remove only its exact SQLite names after checking directory identity.
        assert path.parent.resolve() == path.parent
        assert (path.parent.stat().st_dev, path.parent.stat().st_ino) == directory_identity
        assert path.parent.stat().st_uid == account.pw_uid
        expected = {path.name + suffix for suffix in ("", "-wal", "-shm")}
        assert {p.name for p in path.parent.iterdir()} <= expected
        for name in expected:
            candidate = path.parent / name
            if candidate.exists():
                assert not candidate.is_symlink() and candidate.stat().st_uid == account.pw_uid
                candidate.unlink()
        path.parent.rmdir()

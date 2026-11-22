import json
import sqlite3
from uuid import uuid4

import pytest
from test_native_messages import event
from test_native_messages import execution_case as execution_case
from test_native_messages import message_case as message_case
from test_native_messages import native_case as native_case
from test_native_messages import runtime_case as runtime_case
from test_native_messages import work_case as work_case

from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.execution_lock import execution_lock
from milo_home.native_messages import NativeMessageStore
from milo_home.native_policy import NativePolicyStore
from milo_home.runtime_evidence_backup import capture_runtime, rehearse_runtime


@pytest.fixture
def native_archive_case(message_case, private_runtime_directory):
    runtime, store, scope = message_case
    root = private_runtime_directory("milo-native-runtime-backup-")
    with execution_lock(runtime.database) as lease:
        store.claim(scope, lease=lease)
        store.record(scope, event("sent"), lease=lease)
        store.record(scope, event("stopped", stop_confirmed=True), lease=lease)
    return root, runtime, store, scope


def test_native_ledgers_roundtrip_committed_wal_rules_and_unknown(native_archive_case):
    root, runtime, store, scope = native_archive_case
    policy_store = NativePolicyStore(runtime.journal)
    writers = []
    try:
        for path in (policy_store.path, store.path):
            db = sqlite3.connect(path)
            db.execute("PRAGMA journal_mode=WAL")
            # A committed setting changes the logical database in its actual WAL.
            db.execute("PRAGMA application_id=1234")
            db.commit()
            writers.append(db)
            assert path.with_name(path.name + "-wal").stat().st_size > 0
        home = runtime.journal.path.parent
        before = store.inspect(scope.run_id)
        captured = capture_runtime(home, root / "archive", writers_stopped=True)
        report = rehearse_runtime(root / "archive", root / "restored", captured["manifest_sha256"])
        assert report["restore_verified"] and report["automatic_resume"] is False
        restored = PrivateJournal(root / "restored", initialize=False, read_only=True)
        assert NativeMessageStore(restored).inspect(scope.run_id) == before
        assert (root / "restored/AGENTS.md").read_bytes() == (home / "AGENTS.md").read_bytes()
        for name in (policy_store.path.name, store.path.name):
            with sqlite3.connect(root / "restored" / name) as db:
                assert db.execute("PRAGMA application_id").fetchone()[0] == 1234
            assert not (root / "restored" / (name + "-wal")).exists()
        manifest = json.loads((root / "archive/manifest.json").read_bytes())
        assert manifest["version"] == 2
        assert (
            manifest["native_evidence"]["databases"][store.path.name][
                "without_complete_terminal_and_stop"
            ]
            == 1
        )
        with pytest.raises(ProtocolError, match="installation differs"):
            NativePolicyStore(restored).read()
    finally:
        for db in writers:
            db.close()


@pytest.mark.parametrize(
    "missing", ["native-session-policy.required", "native-message-links.sqlite3"]
)
def test_registered_native_evidence_cannot_be_omitted(native_archive_case, missing):
    root, runtime, _, _ = native_archive_case
    (runtime.journal.path.parent / missing).unlink()
    with pytest.raises(ValueError, match="both exist"):
        capture_runtime(runtime.journal.path.parent, root / "archive", writers_stopped=True)
    assert not (root / "archive").exists()


def test_changed_rules_refuse_archive_without_rebinding(native_archive_case):
    root, runtime, _, _ = native_archive_case
    (runtime.journal.path.parent / "AGENTS.md").write_bytes(b"Changed rules\n")
    with pytest.raises(ValueError, match="instructions"):
        capture_runtime(runtime.journal.path.parent, root / "archive", writers_stopped=True)
    assert not (root / "archive").exists()


def test_tampered_native_ledger_cannot_restore(native_archive_case):
    root, runtime, store, _ = native_archive_case
    captured = capture_runtime(runtime.journal.path.parent, root / "archive", writers_stopped=True)
    (root / "archive/evidence" / store.path.name).write_bytes(b"bad database")
    with pytest.raises(ValueError, match="differs"):
        rehearse_runtime(root / "archive", root / "restored", captured["manifest_sha256"])
    assert not (root / "restored").exists()


@pytest.mark.parametrize("damage", ["public", "linked"])
def test_ignored_native_shm_still_requires_safe_metadata(native_archive_case, damage):
    root, runtime, store, _ = native_archive_case
    path = store.path.with_name(store.path.name + "-shm")
    if damage == "linked":
        path.symlink_to(store.required)
    else:
        path.write_bytes(b"")
        path.chmod(0o644)
    with pytest.raises(ValueError, match="shared memory"):
        capture_runtime(runtime.journal.path.parent, root / "archive", writers_stopped=True)
    assert not (root / "archive").exists()


def test_orphaned_native_mapping_cannot_enter_archive(native_archive_case):
    root, runtime, store, _ = native_archive_case
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO messages VALUES (?,?)", (str(uuid4()), str(uuid4())))
    with pytest.raises(ValueError, match="orphaned"):
        capture_runtime(runtime.journal.path.parent, root / "archive", writers_stopped=True)
    assert not (root / "archive").exists()

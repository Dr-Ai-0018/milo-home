"""Private evidence restoration preserves uncertainty and original identities."""

import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_runtime import bind, journal_at
from test_work_tasks import work_case as work_case

from milo_home.backup_rehearsal import rehearse_backup
from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.database import Database
from milo_home.execution_lock import execution_lock
from milo_home.runtime_evidence_backup import capture_runtime, rehearse_runtime
from milo_home.work_attempt_history import HISTORY
from milo_home.work_runtime import WorkRuntime
from milo_home.workspace_archive_files import write_file

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Private backup requires root")


@pytest.fixture
def runtime_archive_case(private_runtime_directory):
    root = private_runtime_directory("milo-runtime-backup-")
    home = root / "runtime"
    journal = journal_at(home, root)
    stream = journal.open_stream("milo-work-" + "a" * 32 + ".service")
    journal.record(stream, {"kind": "intent", "request": {"id": 1, "method": "turn/start"}})
    journal.record(stream, {"kind": "unknown", "id": 1})
    for name in ("main-registration.json", "main-creation.json"):
        (home / name).write_text('{"synthetic":"original selection"}\n')
        (home / name).chmod(0o600)
    events = home / "main-creation-events"
    events.mkdir(mode=0o700)
    (events / "000001.json").write_text('{"kind":"unknown","id":1}\n')
    (events / "000001.json").chmod(0o600)
    for name in ("auth.json", "credential.env", "a" * 32 + ".env"):
        (home / name).write_text("SECRET_MUST_NOT_ENTER_ARCHIVE")
    # A real committed WAL remains open through capture.
    connection = sqlite3.connect(journal.path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "INSERT INTO entries(stream,payload) VALUES (?,?)",
        (stream, '{"kind":"synthetic-wal-evidence"}'),
    )
    connection.commit()
    yield root, home, journal
    connection.close()


def capture(case):
    root, home, _ = case
    return capture_runtime(home, root / "archive", writers_stopped=True)


def test_roundtrip_retains_wal_unknown_and_original_evidence(runtime_archive_case):
    root, home, journal = runtime_archive_case
    original = journal.outstanding()
    report = capture(runtime_archive_case)
    result = rehearse_runtime(root / "archive", root / "restored", report["manifest_sha256"])
    assert result["restore_verified"] and not result["automatic_resume"]
    restored = PrivateJournal(root / "restored", initialize=False, read_only=True)
    assert restored.outstanding() == original == journal.outstanding()
    assert original[0]["delivery"] == "unknown"
    with restored._connect() as connection:
        assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 3
    with pytest.raises(ProtocolError, match="installation differs"):
        restored.installation()
    for name in (
        "main-registration.json",
        "main-creation.json",
        "main-creation-events/000001.json",
    ):
        assert (root / "restored" / name).read_bytes() == (home / name).read_bytes()
    manifest = json.loads((root / "archive/manifest.json").read_bytes())
    assert manifest["quiescence_verified"] is False
    for path in (root / "archive").rglob("*"):
        if path.is_file():
            assert path.stat().st_mode & 0o777 == 0o600
            assert b"SECRET_MUST_NOT_ENTER_ARCHIVE" not in path.read_bytes()
        else:
            assert path.stat().st_mode & 0o777 == 0o700
    assert not (root / "restored/config.toml").exists()
    assert not (root / "restored/INCOMPLETE").exists()


@pytest.mark.parametrize("name", ["unknown.json", "new.sqlite", "main-creation-events/000003.json"])
def test_unknown_or_nonsequential_evidence_refused(runtime_archive_case, name):
    root, home, _ = runtime_archive_case
    (home / name).write_text("{}")
    (home / name).chmod(0o600)
    with pytest.raises(ValueError, match="Unclassified|sequence"):
        capture(runtime_archive_case)
    assert not (root / "archive").exists()


@pytest.mark.parametrize("linked", [False, True])
def test_public_or_linked_evidence_refused(runtime_archive_case, linked):
    _, home, _ = runtime_archive_case
    path = home / "main-registration.json"
    if linked:
        path.unlink()
        path.symlink_to(home / "main-creation.json")
    else:
        path.chmod(0o644)
    with pytest.raises(ValueError, match="private|symbolic"):
        capture(runtime_archive_case)


@pytest.mark.parametrize("manifest", [False, True])
def test_tampered_artifact_or_manifest_refused(runtime_archive_case, manifest):
    root, _, _ = runtime_archive_case
    result = capture(runtime_archive_case)
    path = root / "archive" / ("manifest.json" if manifest else "evidence/main-creation.json")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="differs"):
        rehearse_runtime(root / "archive", root / "restored", result["manifest_sha256"])
    assert not (root / "restored").exists()


def test_source_change_retains_incomplete_archive(runtime_archive_case, monkeypatch):
    import milo_home.runtime_evidence_backup as module

    root, home, _ = runtime_archive_case
    original = module.sqlite_snapshot

    def changed(*args):
        original(*args)
        (home / "main-creation.json").write_text('{"changed":true}\n')

    monkeypatch.setattr(module, "sqlite_snapshot", changed)
    with pytest.raises(ValueError, match="changed"):
        capture(runtime_archive_case)
    assert (root / "archive/INCOMPLETE").exists()


def test_stop_and_capacity_gates_precede_output(runtime_archive_case):
    root, home, _ = runtime_archive_case
    with pytest.raises(ValueError, match="stopped-writer"):
        capture_runtime(home, root / "archive")
    with pytest.raises(ValueError, match="budget"):
        capture_runtime(home, root / "archive", writers_stopped=True, budget=1)
    assert not (root / "archive").exists()


def test_existing_and_nested_outputs_are_refused(runtime_archive_case):
    root, home, _ = runtime_archive_case
    for output in (root, home, home / "archive"):
        with pytest.raises(ValueError):
            capture_runtime(home, output, writers_stopped=True)


def test_native_material_is_explicitly_assigned_to_separate_archive(runtime_archive_case):
    root, home, _ = runtime_archive_case
    (home / "sessions").mkdir(mode=0o700)
    (home / "sessions/native.jsonl").write_text("native record is captured separately")
    capture(runtime_archive_case)
    manifest = json.loads((root / "archive/manifest.json").read_bytes())
    assert (
        manifest["inventory"]["omitted"]["sessions"] == "separate-native-session-archive-required"
    )
    assert not (root / "archive/evidence/sessions").exists()


def historical_files(home):
    path = home / HISTORY / str(uuid4())
    events = path / "main-creation-events"
    events.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    path.parent.chmod(0o700)
    write_file(path / "main-creation.json", b'{"synthetic": "retired"}\n')
    write_file(events / "000001.json", b'{"kind": "unknown"}\n')
    write_file(events / "000002.json", b'{"kind": "abandoned_before_initialization"}\n')
    return path


def test_runtime_roundtrip_preserves_original_attempt_bytes(runtime_archive_case):
    root, home, _ = runtime_archive_case
    history = historical_files(home)
    report = capture(runtime_archive_case)
    result = rehearse_runtime(root / "archive", root / "restored", report["manifest_sha256"])
    assert result["restore_verified"]
    for path in history.rglob("*.json"):
        name = path.relative_to(home)
        assert (root / "archive/evidence" / name).read_bytes() == path.read_bytes()
        assert (root / "restored" / name).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("unexpected", ["auth.json", "nonconsecutive", "symlink"])
def test_unclassified_history_or_link_cannot_enter_archive(runtime_archive_case, unexpected):
    root, home, _ = runtime_archive_case
    history = historical_files(home)
    events = history / "main-creation-events"
    if unexpected == "auth.json":
        write_file(history / "auth.json", b"excluded fixture")
    elif unexpected == "nonconsecutive":
        (events / "000002.json").rename(events / "000003.json")
    else:
        (events / "000002.json").unlink()
        (events / "000002.json").symlink_to(events / "000001.json")
    with pytest.raises(ValueError, match="history|sequence|private|symbolic"):
        capture(runtime_archive_case)
    assert not (root / "archive").exists()


def test_business_and_runtime_copies_keep_original_binding_and_unknown(
    execution_case, private_runtime_directory
):
    work, _, _, now = execution_case
    root = private_runtime_directory("milo-runtime-business-backup-")
    # The registered workspace is the authority for cwd, not the backup directory.
    with work[0].connect() as connection:
        workspace = connection.execute("SELECT root_path FROM workspaces").fetchone()[0]
    actual = journal_at(root / "actual-runtime", Path(workspace))
    runtime = WorkRuntime(work[0], actual, lambda: now[0])
    _, window = reserve(execution_case)
    with execution_lock(work[0]) as lease:
        linked = bind((execution_case, runtime, actual), lease)
        intent = runtime.claim_start(work[1], window["id"], lease=lease)
    actual.record(
        intent.stream_id,
        {"kind": "intent", "request": {"id": 1, "method": "turn/start"}},
    )
    actual.record(intent.stream_id, {"kind": "unknown", "id": 1})
    snapshot = actual.work_snapshot(intent.stream_id)
    report = capture_runtime(actual.path.parent, root / "archive", writers_stopped=True)
    business = rehearse_backup(work[0].path, root / "business.sqlite3")
    assert business["active_work_windows_retained"] == 1
    rehearse_runtime(root / "archive", root / "restored", report["manifest_sha256"])
    copied = PrivateJournal(root / "restored", initialize=False, read_only=True)
    assert copied.business_binding() == linked
    assert (
        copied.work_snapshot(intent.stream_id) == snapshot == actual.work_snapshot(intent.stream_id)
    )
    assert copied.outstanding()[0]["delivery"] == "unknown"
    database = Database(root / "business.sqlite3")
    with (
        execution_lock(database) as lease,
        pytest.raises(ProtocolError, match="Existing business main association"),
    ):
        WorkRuntime(database, actual).check_session_binding(work[1], work[-1]["id"], lease=lease)

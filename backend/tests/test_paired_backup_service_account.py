"""Whole synthetic generations using an existing low-privilege system account."""

import os
import pwd
import select
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_execution_control import execution_case as execution_case
from test_paired_backup import capture, generation_case
from test_work_tasks import work_case as work_case

from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.database import Database
from milo_home.database_identity import ServiceDatabase
from milo_home.execution_control import ExecutionControl
from milo_home.execution_lock import execution_lock
from milo_home.paired_backup import rehearse_generation
from milo_home.schedules import ScheduleStore
from milo_home.work_access import WorkAccess
from milo_home.work_runtime import WorkRuntime
from milo_home.work_tasks import WorkTasks
from milo_home.workspace_git import git

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Service account requires root")
PREFIX = "milo-paired-service-"
SQLITE_NAMES = {"home.sqlite3" + suffix for suffix in ("", "-wal", "-shm")}


def clean_source(directory, identity, uid):
    """Remove only this fixture's exact SQLite files; retain anything unexpected."""
    metadata = directory.lstat()
    if (
        os.geteuid() != 0
        or directory.parent != Path("/tmp")
        or not directory.name.startswith(PREFIX)
        or directory.resolve() != directory
        or not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != identity
        or metadata.st_uid != uid
        or metadata.st_mode & 0o077
    ):
        raise ValueError("Synthetic source directory identity differs")
    children = list(directory.iterdir())
    if {path.name for path in children} - SQLITE_NAMES:
        raise ValueError("Unexpected synthetic source child")
    for path in children:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != uid
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
        ):
            raise ValueError("Synthetic source file identity differs")
    for path in children:
        path.unlink()
    directory.rmdir()


@pytest.fixture
def service_case(execution_case, tmp_path, private_runtime_directory):
    try:
        account = pwd.getpwnam("nobody")
    except KeyError:
        pytest.skip("A low-privilege nobody account is not present")
    work, _, _, now = execution_case
    directory = Path(tempfile.mkdtemp(prefix=PREFIX, dir="/tmp"))
    metadata = directory.stat()
    identity = metadata.st_dev, metadata.st_ino
    path = directory / "home.sqlite3"
    # Transfer only a fresh synthetic database, never an installed service file.
    work[0].backup(path)
    os.chown(path, account.pw_uid, account.pw_gid)
    os.chown(directory, account.pw_uid, account.pw_gid)
    try:
        database = ServiceDatabase(path, account.pw_name)

        def clock():
            return now[0]

        new_work = (
            database,
            *work[1:4],
            WorkAccess(database, clock=clock),
            WorkTasks(database, clock=clock),
            work[-1],
        )
        case = (
            new_work,
            ExecutionControl(database, clock),
            ScheduleStore(database, clock),
            now,
        )
        yield generation_case(case, tmp_path, private_runtime_directory), account
    finally:
        clean_source(directory, identity, account.pw_uid)


@contextmanager
def committed_wal(database, account):
    script = (
        "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
        "c.execute(\"INSERT INTO auth_buckets VALUES ('synthetic-whole-wal',1,1)\"); "
        "c.commit(); print('READY',flush=True); sys.stdin.readline(); c.close()"
    )
    writer = subprocess.Popen(
        ["/usr/bin/python3", "-c", script, str(database.path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        user=account.pw_uid,
        group=account.pw_gid,
        extra_groups=[],
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    try:
        assert select.select([writer.stdout], [], [], 3)[0], "WAL holder did not become ready"
        assert writer.stdout.readline().strip() == "READY"
        assert database.path.with_name(database.path.name + "-wal").stat().st_size > 0
        yield
    finally:
        writer.stdin.close()
        try:
            assert writer.wait(timeout=3) == 0
        except subprocess.TimeoutExpired:
            writer.kill()
            writer.wait(timeout=3)
            raise
        finally:
            writer.stdout.close()
            writer.stderr.close()


def assert_private_tree(directory, *, restored_workspace=None):
    for path in (directory, *directory.rglob("*")):
        metadata = path.lstat()
        assert metadata.st_uid == 0
        if (
            restored_workspace is not None
            and path != restored_workspace
            and path.is_relative_to(restored_workspace)
        ):
            # Git restores original file modes behind a private workspace root.
            continue
        assert stat.S_IMODE(metadata.st_mode) == (0o700 if path.is_dir() else 0o600)


def test_actual_account_whole_generation_preserves_wal_unknown_and_git(service_case):
    case, account = service_case
    root, database, home, workspace, journal, intent = case
    original_identity = os.geteuid(), os.getegid()
    original = journal.work_snapshot(intent.stream_id)
    with committed_wal(database, account):
        captured = capture(case)
        report = rehearse_generation(
            root / "archive", root / "restored", captured["manifest_sha256"]
        )
        assert (os.geteuid(), os.getegid()) == original_identity
        for path in database.path.parent.iterdir():
            assert path.stat().st_uid == account.pw_uid
            assert not path.stat().st_mode & 0o077
        assert_private_tree(root / "archive")
        assert_private_tree(root / "restored", restored_workspace=root / "restored/workspace")
        with Database(root / "restored/business.sqlite3").connect() as connection:
            assert connection.execute("SELECT key_hash FROM auth_buckets").fetchone()[0] == (
                "synthetic-whole-wal"
            )
        assert report["file_restore_verified"] is True
        assert report["business"]["active_work_windows_retained"] == 1
        for field in (
            "quiescence_verified",
            "protocol_restore_verified",
            "execution_binding_verified",
            "remote_verified",
            "automatic_resume",
        ):
            assert report[field] is False
        restored = PrivateJournal(root / "restored/runtime", initialize=False, read_only=True)
        assert restored.work_snapshot(intent.stream_id) == original
        assert journal.work_snapshot(intent.stream_id) == original
        assert restored.outstanding()[0]["delivery"] == "unknown"
        assert (root / "restored/native/sessions/rollout.jsonl").read_bytes() == (
            home / "sessions/rollout.jsonl"
        ).read_bytes()
        recovered_workspace = root / "restored/workspace"
        for name in ("work.txt", "extra.txt"):
            assert (recovered_workspace / name).read_bytes() == (workspace / name).read_bytes()
        assert git(recovered_workspace, "diff", "--cached") == git(workspace, "diff", "--cached")
        assert git(recovered_workspace, "diff") == git(workspace, "diff")
        copied = Database(root / "restored/business.sqlite3")
        linked = journal.business_binding()
        with (
            execution_lock(copied) as lease,
            pytest.raises(ProtocolError, match="Existing business main association"),
        ):
            WorkRuntime(copied, journal).check_session_binding(
                linked.owner_id, linked.session_id, lease=lease
            )
        assert not (root / "archive/INCOMPLETE").exists()
        assert not (root / "restored/INCOMPLETE").exists()


@pytest.mark.parametrize("failure", ["business-budget", "native"])
def test_failed_whole_capture_restores_identity_and_keeps_incomplete(
    service_case, monkeypatch, failure
):
    import milo_home.paired_backup as module
    import milo_home.paired_backup_components as components

    case, account = service_case
    root, database, _, _, journal, intent = case
    identity = os.geteuid(), os.getegid()
    original = journal.work_snapshot(intent.stream_id)
    snapshot = module.business_snapshot

    def fail_snapshot(database, destination, budget):
        return snapshot(database, destination, 1)

    def fail_native(*args, **kwargs):
        raise OSError("Synthetic native capture failure")

    if failure == "business-budget":
        monkeypatch.setattr(module, "business_snapshot", fail_snapshot)
        exception, message = ValueError, "budget"
    else:
        monkeypatch.setattr(components, "capture_native", fail_native)
        exception, message = OSError, "Synthetic native"
    with committed_wal(database, account), pytest.raises(exception, match=message):
        capture(case)
    assert (os.geteuid(), os.getegid()) == identity
    assert (root / "archive/INCOMPLETE").exists()
    assert not (root / "archive/manifest.json").exists()
    assert_private_tree(root / "archive")
    assert journal.work_snapshot(intent.stream_id) == original
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM auth_buckets").fetchone()[0] == 1


@pytest.mark.parametrize("obstruction", ["identity", "extra", "link", "mode"])
def test_synthetic_cleanup_refuses_unexpected_material(obstruction):
    directory = Path(tempfile.mkdtemp(prefix=PREFIX, dir="/tmp"))
    metadata = directory.stat()
    identity = metadata.st_dev, metadata.st_ino
    path = directory / "home.sqlite3"
    try:
        path.write_bytes(b"synthetic")
        path.chmod(0o600)
        if obstruction == "extra":
            (directory / "unrelated").write_bytes(b"retain")
        elif obstruction == "link":
            path.unlink()
            path.symlink_to("/nonexistent-synthetic-target")
        elif obstruction == "mode":
            path.chmod(0o644)
        wrong_identity = (identity[0], identity[1] + 1)
        with pytest.raises(ValueError, match="Synthetic|Unexpected"):
            clean_source(directory, wrong_identity if obstruction == "identity" else identity, 0)
        assert path.exists() or path.is_symlink()
    finally:
        # Only the two literal names created above; no recursive fallback.
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            path.chmod(0o600)
        extra = directory / "unrelated"
        if extra.exists():
            extra.unlink()
        clean_source(directory, identity, 0)

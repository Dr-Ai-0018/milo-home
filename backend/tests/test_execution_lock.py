import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from milo_home import execution_lock as module
from milo_home.database import Database
from milo_home.errors import ApiError
from milo_home.execution_lock import ExecutionIdentityError, execution_lock
from milo_home.worker import worker_lock

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Host coordination requires root")


@pytest.fixture
def database(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir(mode=0o700)
    value = Database(directory / "home.sqlite3")
    value.initialize()
    return value


CHILD = """
import sys
from pathlib import Path
from milo_home.database import Database
from milo_home.worker import worker_lock
from milo_home.errors import ApiError
try:
    with worker_lock(Database(Path(sys.argv[1])), Path(sys.argv[2])):
        print('acquired', flush=True)
        if sys.argv[3] == 'hold':
            sys.stdin.read()
except ApiError as error:
    print(error.code, flush=True)
"""


def contender(database, runtime):
    result = subprocess.run(
        [sys.executable, "-c", CHILD, str(database.path), str(runtime), "once"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_different_runtime_cannot_bypass_shared_lock_across_processes(database, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    with worker_lock(database, first):
        assert contender(database, second) == "worker_busy"
    assert contender(database, second) == "acquired"
    assert not (database.path.parent / "worker.lock").exists()


@pytest.mark.parametrize(
    "holder,contender", [(execution_lock, worker_lock), (worker_lock, execution_lock)]
)
def test_new_and_legacy_entries_exclude_each_other(database, holder, contender):
    with holder(database):
        with pytest.raises(ApiError, match="worker_busy"):
            with contender(database):
                pytest.fail("New and legacy coordination overlapped")


def test_alias_and_hardlink_cannot_bypass_inode_lock(database, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(database.path.parent, target_is_directory=True)
    hardlink = database.path.parent / "other.sqlite3"
    os.link(database.path, hardlink)
    with execution_lock(database):
        for path in (alias / database.path.name, hardlink):
            with pytest.raises(ApiError, match="worker_busy"):
                with execution_lock(Database(path)):
                    pytest.fail("Alias acquired an occupied database")
    # The failed hardlink contender acquired its path socket first; it must
    # release that partial acquisition when the inode socket is already held.
    with execution_lock(Database(hardlink)) as lease:
        lease.verify()


def test_exception_releases_lock_and_invalidates_lease(database):
    with pytest.raises(RuntimeError, match="synthetic"):
        with execution_lock(database) as lease:
            raise RuntimeError("synthetic")
    with pytest.raises(ExecutionIdentityError, match="no longer"):
        lease.verify()
    with execution_lock(database):
        pass


def test_abrupt_coordinator_exit_releases_only_host_lock(database, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    process = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(database.path), str(runtime), "hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        import select

        assert select.select([process.stdout], [], [], 3)[0], "Child failed to acquire in time"
        assert process.stdout.readline().strip() == "acquired"
        assert contender(database, runtime) == "worker_busy"
        process.kill()
        process.wait(timeout=3)
        assert contender(database, runtime) == "acquired"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)


def test_replaced_database_cannot_bypass_path_lock_or_pass_verification(database):
    original = database.path.with_suffix(".original")
    with execution_lock(database) as lease:
        database.path.rename(original)
        database.path.touch(mode=0o600)
        with pytest.raises(ExecutionIdentityError, match="identity changed"):
            lease.verify()
        with pytest.raises(ApiError, match="worker_busy"):
            with execution_lock(database):
                pytest.fail("Replacement bypassed the canonical path lock")


@pytest.mark.parametrize("mode", [0o640, 0o666])
def test_public_database_refused(database, mode):
    database.path.chmod(mode)
    with pytest.raises(ExecutionIdentityError, match="private ordinary"):
        with execution_lock(database):
            pass


def test_namespace_and_nonroot_refused_before_acquisition(database, monkeypatch):
    real_stat = module.os.stat

    def changed_namespace(path, *args, **kwargs):
        if path == "/proc/self/ns/net":
            return SimpleNamespace(st_dev=1, st_ino=1)
        if path == "/proc/1/ns/net":
            return SimpleNamespace(st_dev=1, st_ino=2)
        return real_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "stat", changed_namespace)
        with pytest.raises(ExecutionIdentityError, match="network namespace"):
            with execution_lock(database):
                pass
    monkeypatch.setattr(module.os, "geteuid", lambda: 65534)
    with pytest.raises(ValueError, match="requires root"):
        with execution_lock(database):
            pass


def test_legacy_lock_failure_releases_shared_marker(database):
    legacy = database.path.parent / "worker.lock"
    legacy.symlink_to(database.path)
    with pytest.raises(OSError):
        with worker_lock(database):
            pass
    with execution_lock(database):
        pass


def test_exec_child_does_not_keep_parent_lock_alive(database, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    process = None
    try:
        with execution_lock(database):
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"], close_fds=False
            )
        assert process.poll() is None
        assert contender(database, runtime) == "acquired"
    finally:
        if process is not None:
            process.kill()
            process.wait(timeout=3)


def test_missing_database_and_inherited_lease_fail_closed(database, monkeypatch):
    with execution_lock(database) as lease:
        with monkeypatch.context() as patch:
            patch.setattr(module.os, "getpid", lambda: lease.pid + 1)
            with pytest.raises(ExecutionIdentityError, match="no longer"):
                lease.verify()
        database.path.unlink()
        with pytest.raises(ExecutionIdentityError, match="unavailable"):
            lease.verify()


def test_public_directory_or_wrong_owner_is_refused(database):
    database.path.parent.chmod(0o750)
    with pytest.raises(ExecutionIdentityError, match="directory"):
        with execution_lock(database):
            pass
    database.path.parent.chmod(0o700)
    os.chown(database.path, 65534, 65534)
    with pytest.raises(ExecutionIdentityError, match="account"):
        with execution_lock(database):
            pass

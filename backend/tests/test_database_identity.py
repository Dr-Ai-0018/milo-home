import os
import pwd
import signal
import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest

from milo_home.database import Database
from milo_home.database_identity import ServiceDatabase, private_run_directory
from milo_home.scheduler_cli import StopRequest, stop_signals

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Real identity check requires root")


@pytest.fixture
def owned_database():
    account = pwd.getpwnam("nobody")
    directory = Path(tempfile.mkdtemp(prefix="milo-identity-test-"))
    path = directory / "home.sqlite3"
    Database(path).initialize()
    for entry in [directory, *directory.iterdir()]:
        os.chown(entry, account.pw_uid, account.pw_gid)
    # Retain the small synthetic directory rather than recursively cleaning
    # paths while a test is deliberately changing Unix identities.
    return path, account


def test_sqlite_wal_owner_and_identity_restore_on_success_and_failure(owned_database):
    path, account = owned_database
    database = ServiceDatabase(path, account.pw_name)
    original = os.geteuid(), os.getegid()
    database.initialize()
    with database.connect(write=True) as connection:
        assert (os.geteuid(), os.getegid()) == (account.pw_uid, account.pw_gid)
        connection.execute("INSERT INTO auth_buckets VALUES ('synthetic', 1, 1)")
        for entry in path.parent.iterdir():
            assert entry.stat().st_uid == account.pw_uid
    assert (os.geteuid(), os.getegid()) == original
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with database.connect(write=True) as connection:
            connection.execute("DELETE FROM auth_buckets")
            raise RuntimeError("synthetic failure")
    assert (os.geteuid(), os.getegid()) == original
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM auth_buckets").fetchone()[0] == 1


def test_identity_switch_rejects_threads_without_changing_credentials(owned_database):
    path, account = owned_database
    database = ServiceDatabase(path, account.pw_name)
    original = os.geteuid(), os.getegid()
    done = threading.Event()
    thread = threading.Thread(target=lambda: done.wait(10))
    thread.start()
    try:
        with pytest.raises(ValueError, match="single-threaded"):
            with database.connect():
                pass
    finally:
        done.set()
        thread.join()
    assert (os.geteuid(), os.getegid()) == original


def test_owner_and_link_rejections(owned_database):
    path, account = owned_database
    alias = path.parent / "linked.sqlite3"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="linked"):
        ServiceDatabase(alias, account.pw_name)
    os.chown(path, 0, 0)
    with pytest.raises(ValueError, match="owned"):
        ServiceDatabase(path, account.pw_name)
    with pytest.raises(ValueError, match="non-root"):
        ServiceDatabase(path, "root")


def test_shared_execution_lock_leaves_service_directory_account_owned(owned_database, tmp_path):
    from milo_home.worker import worker_lock

    path, account = owned_database
    database = ServiceDatabase(path, account.pw_name)
    runtime = tmp_path / "root-runtime"
    runtime.mkdir(mode=0o700)
    with worker_lock(database, runtime) as lease:
        with database.connect() as connection:
            assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        lease.verify()
        assert os.geteuid() == 0
    assert all(entry.stat().st_uid == account.pw_uid for entry in path.parent.iterdir())
    assert (runtime / "worker.lock").stat().st_uid == 0


def test_runtime_rejects_service_owned_or_replaceable_paths(owned_database):
    path, _ = owned_database
    with pytest.raises(ValueError, match="root-owned"):
        private_run_directory(path.parent)
    nested = path.parent / "root-runtime"
    nested.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="ancestors"):
        private_run_directory(nested)
    with pytest.raises(ValueError, match="absolute"):
        private_run_directory(Path("relative"))


def test_managed_signal_during_database_access_preserves_identity_and_connection(owned_database):
    path, account = owned_database
    database = ServiceDatabase(path, account.pw_name)
    original = os.geteuid(), os.getegid()
    stop = StopRequest()
    with stop_signals(stop):
        with database.connect() as connection:
            signal.raise_signal(signal.SIGTERM)
            assert stop.is_set()
            assert (os.geteuid(), os.getegid()) == (account.pw_uid, account.pw_gid)
            assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert (os.geteuid(), os.getegid()) == original


def test_management_progress_renews_before_run_transaction_as_service_owner(owned_database):
    from milo_home.automatic_budget import AutomaticPolicy
    from milo_home.managed_stop import ManagedStop
    from milo_home.management_lease import ManagementLease
    from milo_home.runs import RunStore

    path, account = owned_database
    database = ServiceDatabase(path, account.pw_name)
    original = os.geteuid(), os.getegid()
    now = [100]
    lease = ManagementLease(
        database,
        AutomaticPolicy(daily_launches=2, daily_seconds=120),
        clock=lambda: now[0],
        monotonic=lambda: now[0],
    )
    lease.claim()
    managed = ManagedStop(StopRequest(), lease)
    now[0] += 15
    store = RunStore(database, clock=lambda: now[0], progress=managed.progress)
    assert store.started("missing-synthetic-run") is None
    with database.connect() as connection:
        assert connection.execute("SELECT heartbeat_at FROM management_lease").fetchone()[0] == 115
        assert os.geteuid() == account.pw_uid
    assert (os.geteuid(), os.getegid()) == original
    assert all(entry.stat().st_uid == account.pw_uid for entry in path.parent.iterdir())
    lease.close()


def test_owner_snapshot_restores_uid_on_success_and_exception(owned_database):
    path, account = owned_database
    database = ServiceDatabase(path, account.pw_name)
    original = os.geteuid(), os.getegid()
    with database.snapshot() as connection:
        assert os.geteuid() == account.pw_uid
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM users")
    assert (os.geteuid(), os.getegid()) == original
    with pytest.raises(RuntimeError, match="snapshot-failure"):
        with database.snapshot():
            raise RuntimeError("snapshot-failure")
    assert (os.geteuid(), os.getegid()) == original

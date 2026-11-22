import json
from uuid import uuid4

import pytest
from test_runs import pending

from milo_home import worker as module
from milo_home.errors import ApiError
from milo_home.execution_lock import ExecutionIdentityError
from milo_home.run_transport import EventStream
from milo_home.worker import Worker, worker_lock


def fake_runtime(base, snapshot, profile):
    root = base / "rootfs"
    (root / "runtime").mkdir(parents=True, mode=0o700)
    (base / "prompt.txt").write_text("synthetic")
    (base / "credential.env").write_text("synthetic")
    answer = {
        "replies": [
            {
                "message_id": message["id"],
                "body": "模拟执行器的测试回信",
            }
            for message in snapshot["selected"]
        ]
    }
    (root / "runtime/answer.json").write_text(json.dumps(answer))
    return root


@pytest.mark.parametrize("cancel,invalid", [(False, False), (True, False), (False, True)])
def test_worker_publishes_only_valid_uncancelled_results(client, monkeypatch, cancel, invalid):
    thread, store = pending(client, 1)
    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)

    def execute(command, prompt, store, run_id):
        store.started(run_id)
        if cancel:
            store.stop(run_id)
        if invalid:
            answer = prompt.parent / "rootfs/runtime/answer.json"
            content = json.loads(answer.read_text())
            content["replies"][0]["message_id"] = str(uuid4())
            answer.write_text(json.dumps(content))
        events = EventStream()
        events.completed = 1
        return 0, events, True

    monkeypatch.setattr(module, "execute", execute)
    run = Worker(store.database).wake(thread, 60)
    expected = "cancelled" if cancel else "failed" if invalid else "succeeded"
    assert run["status"] == expected
    assert not (store.database.path.parent / "runs" / run["id"] / "credential.env").exists()
    assert client.get("/api/threads").json()[0]["pending_count"] == (expected != "succeeded")


def test_unknown_termination_retains_occupancy_and_lock_excludes_workers(client, monkeypatch):
    thread, store = pending(client, 1)
    monkeypatch.setattr(module, "prepare", fake_runtime)
    monkeypatch.setattr(module, "execute", lambda *args: (0, EventStream(), False))
    with pytest.raises(ApiError, match="termination_unknown"):
        Worker(store.database).wake(thread, 60)
    assert store.active()["status"] == "stopping"
    with pytest.raises(ApiError, match="run_busy"):
        Worker(store.database).wake(thread, 60)
    with worker_lock(store.database):
        with pytest.raises(ApiError, match="worker_busy"):
            with worker_lock(store.database):
                pass


def test_recovery_confirms_process_end_before_releasing(client, monkeypatch):
    thread, store = pending(client, 1)
    run = store.queue(thread, 60)
    store.stop(run)
    monkeypatch.setattr(module, "stop_unit", lambda unit: False)
    with pytest.raises(ApiError, match="termination_unknown"):
        Worker(store.database).recover()
    assert store.active()["id"] == run
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)
    assert Worker(store.database).recover() == run
    assert store.get(run)["status"] == "cancelled"
    assert store.active() is None


def test_explicit_runtime_uses_separate_lock_and_recovery_credentials(
    client, monkeypatch, tmp_path
):
    thread, store = pending(client, 1)
    runtime = tmp_path / "root-runtime"
    runtime.mkdir(mode=0o700)
    worker = Worker(store.database, runtime=runtime)
    run_id = store.queue(thread, 60)
    run_directory = runtime / run_id
    run_directory.mkdir(mode=0o700)
    credential = run_directory / "credential.env"
    credential.write_text("synthetic only")
    monkeypatch.setattr(module, "stop_unit", lambda unit: True)
    assert worker.recover() == run_id
    assert not credential.exists()
    assert (runtime / "worker.lock").exists()
    assert not (store.database.path.parent / "worker.lock").exists()
    assert store.active() is None


def test_database_replaced_during_preparation_never_launches_or_updates_replacement(
    client, monkeypatch
):
    from milo_home.database import Database

    thread, store = pending(client, 1)
    path = store.database.path
    parked = path.with_suffix(".parked")

    def replace(base, snapshot, profile):
        root = fake_runtime(base, snapshot, profile)
        path.rename(parked)
        Database(path).initialize()
        return root

    monkeypatch.setattr(module, "prepare", replace)
    monkeypatch.setattr(module, "execute", lambda *args: pytest.fail("Replacement launched"))
    with pytest.raises(ExecutionIdentityError, match="identity changed"):
        Worker(store.database).wake(thread, 60)
    with store.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    with Database(parked).connect() as connection:
        assert connection.execute("SELECT status FROM runs").fetchone()[0] == "queued"
        assert connection.execute("SELECT authorized_at FROM legacy_launches").fetchone()[0] is None

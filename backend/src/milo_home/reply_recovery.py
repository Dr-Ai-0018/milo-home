"""Explicit stop, publication-only replay, and confirmed failure recovery."""

from .dispatch import DispatchStore
from .errors import ApiError
from .native_reply_recovery import reconcile_native_reply
from .reply_receipts import publish_receipt
from .run_transport import unit_empty
from .runs import RunStore
from .worker import Worker, validate_unit, worker_lock


def owned_run(database, owner, run_id):
    with database.snapshot() as connection:
        row = connection.execute(
            "SELECT runs.* FROM runs JOIN reply_run_scopes ON reply_run_scopes.run_id=runs.id "
            "WHERE runs.id=? AND reply_run_scopes.owner_id=?",
            (run_id, owner),
        ).fetchone()
        if row is None:
            raise ApiError(404, "reply_run_missing", "没有找到本账号的独立回信运行。")
        return dict(row)


def request_stop(database, owner, run_id):
    owned_run(database, owner, run_id)
    RunStore(database).stop(run_id)
    return {"outcome": "stop_requested"}


def replay(database, directory, owner, run_id):
    with worker_lock(database, directory) as lease:
        run = owned_run(database, owner, run_id)
        worker = Worker(database, runtime=directory)
        reconcile_native_reply(database, worker._base(run_id), owner, run, lease)
        if not unit_empty(validate_unit(run)):
            raise ApiError(503, "termination_unknown", "原执行组尚未确认停止，不能补投。")
        worker._remove_credential(run_id)
        published = publish_receipt(database, owner, run_id)
        lease.verify()
        DispatchStore(database).reconcile()
        return {"outcome": "succeeded" if published else "publication_blocked"}


def recover(database, directory, owner, run_id):
    with worker_lock(database, directory) as lease:
        run = owned_run(database, owner, run_id)
        with database.snapshot() as connection:
            sealed = connection.execute(
                "SELECT 1 FROM reply_execution_results WHERE run_id=?", (run_id,)
            ).fetchone()
            if sealed and not run["stop_reason"]:
                raise ApiError(409, "reply_result_sealed", "已有确认结果，请先走仅补发布入口。")
        if run["status"] in {"queued", "running", "stopping"}:
            worker = Worker(database, runtime=directory)
            native = reconcile_native_reply(database, worker._base(run_id), owner, run, lease)
            if native:
                worker.store.stop(run_id, "interrupted")
                worker.store.finish_failed(run_id, "interrupted")
            else:
                worker._recover_locked(run)
        lease.verify()
        DispatchStore(database).reconcile()
    return {"outcome": "reconciled_without_relaunch"}

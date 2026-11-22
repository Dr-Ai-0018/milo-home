"""One owner-scoped batch with no unattended recovery or environment fallback."""

import sqlite3
import time
from contextlib import ExitStack

from .automatic_services import AutomaticServices, require_entry
from .errors import ApiError
from .management_lease import current_boot
from .reply_readiness import inspect_preparation
from .reply_receipts import publish_receipt, seal_result
from .reply_scope import ReplyScope
from .run_environment import prepare
from .worker import Worker, worker_lock


class ReplyWorker(Worker):
    def __init__(
        self,
        database,
        profile,
        config,
        credential,
        owner,
        *,
        runtime,
        clock=time.time,
        stop_requested=None,
        progress=None,
    ):
        super().__init__(
            database,
            profile,
            clock,
            runtime=runtime,
            scope=ReplyScope(owner),
            stop_requested=stop_requested,
            progress=progress,
        )
        self.config = config
        self.credential = credential
        self.owner = owner

    def _prepare_run(self, run_id):
        return prepare(
            self._base(run_id),
            self.store.snapshot(run_id),
            self.profile,
            credential=self.credential,
            configuration=self.config.content,
        )

    def _admission_blocker(self, *, lease):
        return None

    def _publish_result(self, run_id, result, code, events, ended):
        self.store.checkpoint(run_id)
        if not seal_result(
            self.database,
            run_id,
            result,
            code=code,
            events=events,
            ended=ended,
            clock=self.store.clock,
        ):
            return False
        try:
            return publish_receipt(self.database, self.owner, run_id, clock=self.store.clock)
        except (OSError, ValueError, sqlite3.Error):
            raise ApiError(503, "publication_pending", "结果已经确认，等待仅补发布。") from None

    def take_next(self, timeout=3600):
        services = AutomaticServices(self.database, self.store.clock)
        with ExitStack() as stack:
            try:
                lease = stack.enter_context(worker_lock(self.database, self.runtime))
            except ApiError as error:
                if error.code != "worker_busy":
                    raise
                return {"outcome": "worker_busy"}
            lease.verify()
            self.dispatch.reconcile()
            with self.database.snapshot() as connection:
                entry = require_entry(connection, "reply", self.owner)
                if not entry["enabled"] or entry["fault_code"]:
                    return {"outcome": "service_disabled_or_faulted"}
                orphan = connection.execute(
                    "SELECT 1 FROM runs JOIN reply_run_scopes ON reply_run_scopes.run_id=runs.id "
                    "WHERE reply_run_scopes.owner_id=? AND runs.status IN "
                    "('queued','running','stopping') LIMIT 1",
                    (self.owner,),
                ).fetchone()
                if (
                    connection.execute(
                        "SELECT 1 FROM automatic_services WHERE kind='work' AND owner_id=?",
                        (self.owner,),
                    ).fetchone()
                    is None
                ):
                    raise ApiError(
                        409, "work_service_unregistered", "先登记现有交办入口，再启用共同排队。"
                    )
                report = inspect_preparation(
                    connection,
                    self.dispatch.scope,
                    int(self.store.clock()),
                    timeout,
                    current_boot(),
                )
            if orphan:
                services.fault("reply", self.owner, "needs_reconciliation")
                return {"outcome": "needs_reconciliation"}
            if report["blockers"]:
                if "needs_reconciliation" in report["blockers"]:
                    services.fault("reply", self.owner, "needs_reconciliation")
                return {"outcome": report["blockers"][0]}
            if self.store.stop_requested is not None and self.store.stop_requested():
                return {"outcome": "stop_requested"}
            blocker = self._admission_blocker(lease=lease)
            if blocker:
                return {"outcome": blocker}
            ticket = self.dispatch.reserve(timeout)
            if ticket is None:
                return {"outcome": "waiting_or_other_entry_first"}
            try:
                run = self._execute(ticket.run_id, ticket, lease=lease)
            finally:
                lease.verify()
                self.dispatch.reconcile()
            if run["status"] not in {"succeeded", "cancelled"}:
                fault = run["failure_code"]
                if fault not in {"execution_failed", "configuration_error", "invalid_result"}:
                    fault = "interrupted"
                services.fault("reply", self.owner, fault)
            return {"outcome": run["status"]}

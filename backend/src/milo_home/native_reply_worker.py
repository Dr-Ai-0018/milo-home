"""Reuse ordinary reply selection/publication while executing in the registered native main."""

import sqlite3

from .codex_rpc import DeliveryUnknown, ProtocolError, RequestRejected
from .errors import ApiError
from .journal_capacity import can_admit
from .native_messages import NativeMessageStore
from .native_reply_process import NativeReplyProcess
from .native_reply_results import seal_native_result
from .reply_receipts import publish_receipt
from .reply_worker import ReplyWorker
from .work_runtime import configured_native_runtime


class NativeReplyWorker(ReplyWorker):
    def __init__(self, *args, native_home, model=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.dispatch.include_revisits = True
        self.native_runtime = configured_native_runtime(
            self.database,
            native_home,
            self.config,
            model=model,
            clock=self.store.clock,
        )

    def _admission_blocker(self, *, lease):
        lease.verify()
        journal = self.native_runtime.journal
        if (
            not can_admit(journal.capacity())
            or not NativeMessageStore(journal).capacity()["has_capacity"]
        ):
            return "native_storage_capacity"
        return None

    def _execute(self, run_id, ticket=None, *, lease):
        if ticket is None or ticket.run_id != run_id:
            raise ProtocolError("Native ordinary replies require their original dispatch ticket")
        live = NativeReplyProcess(self, run_id, ticket, lease=lease)
        result = None
        try:
            result = live.execute()
            if result is not None:
                self.store.checkpoint(run_id)
                if seal_native_result(
                    self.native_runtime, live.delivery.scope, result, lease=lease
                ):
                    try:
                        if publish_receipt(
                            self.database, self.owner, run_id, clock=self.store.clock
                        ):
                            return self.store.get(run_id)
                    except (OSError, ValueError, sqlite3.Error):
                        raise ApiError(
                            503, "publication_pending", "结果已经确认，等待仅补发布。"
                        ) from None
        except KeyboardInterrupt:
            self.store.stop(run_id, "cancelled")
        except ApiError as error:
            if error.code != "reply_stop_requested":
                raise
        except (
            OSError,
            ValueError,
            sqlite3.Error,
            ProtocolError,
            DeliveryUnknown,
            RequestRejected,
        ):
            if live.closed is not True:
                self.store.stop(run_id, "interrupted")
                raise ApiError(
                    503, "needs_reconciliation", "原生回信仍待核对，不会重投。"
                ) from None
        failed_output = (
            live.delivery is not None and live.delivery.conversation.terminal == "completed"
        )
        failure = "invalid_result" if failed_output or result is not None else live.failure_code
        self.store.finish_failed(run_id, failure)
        return self.store.get(run_id)

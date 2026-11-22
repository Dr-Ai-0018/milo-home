"""One saved website batch enters the existing native thread exactly once."""

from uuid import UUID

from .codex_app_server import MainConversation
from .codex_rpc import ProtocolError
from .native_messages import NativeMessageEvent, NativeMessageScope, NativeMessageStore
from .native_reply_input import NATIVE_REPLY_INPUT_LIMIT, native_input_digest, native_reply_input
from .native_reply_results import final_result, result_digest
from .reply_results import CHOICE_OUTPUT_SCHEMA
from .reply_scope import ReplyScope


class NativeReplyDelivery:
    def __init__(
        self, runtime, owner, run_id, store, rpc, stream_id, *, lease, authorize, output_policy
    ):
        self.runtime = runtime
        self.owner = owner
        self.run_id = run_id
        self.store = store
        self.rpc = rpc
        self.stream_id = stream_id
        self.lease = lease
        self.authorize = authorize
        self.output_policy = output_policy
        self.request_id = None
        self.accepted = None
        self.authorized = False
        self.links = NativeMessageStore(runtime.journal)
        linked = runtime.journal.business_binding()
        if linked is None or runtime.native_policy is None:
            raise ProtocolError("An explicitly configured native main is required")
        self.session_id = linked.session_id
        self.access = runtime.check_session_binding(owner, self.session_id, lease=lease)[1:]
        self.prompt = self._saved_input()
        self.scope = NativeMessageScope(
            run_id=UUID(run_id),
            owner_id=UUID(owner),
            conversation_id=UUID(self.snapshot["conversation_id"]),
            business_binding_id=UUID(linked.binding_id),
            baseline_fingerprint=runtime.journal.fingerprint(),
            policy_digest=runtime.native_policy.digest,
            native_thread_id=runtime.conversation_binding().thread_id,
            message_ids=tuple(UUID(item["id"]) for item in self.snapshot["selected"]),
            input_digest=native_input_digest(self.prompt),
            revisit=self.snapshot.get("revisit"),
        )
        self.links.claim(self.scope, lease=lease)
        rpc.record = self.record
        self.conversation = MainConversation(
            rpc,
            runtime.conversation_binding(),
            authorize_turn=self._authorize,
            input_limit=NATIVE_REPLY_INPUT_LIMIT,
            execution_policy=runtime.execution_policy,
        )

    def _saved_input(self):
        with self.runtime.database.snapshot() as connection:
            run = connection.execute(
                "SELECT runs.* FROM runs JOIN reply_run_scopes ON reply_run_scopes.run_id=runs.id "
                "WHERE runs.id=? AND reply_run_scopes.owner_id=?",
                (self.run_id, self.owner),
            ).fetchone()
            if run is None or run["status"] not in {"queued", "running"} or run["stop_reason"]:
                raise ProtocolError("Saved native reply is not available for this owner")
            ReplyScope(self.owner).require_thread(connection, run["thread_id"])
            expected = {
                row[0]
                for row in connection.execute(
                    "SELECT message_id FROM run_messages WHERE run_id=?",
                    (self.run_id,),
                )
            }
        snapshot = self.store.snapshot(self.run_id, include_history=False)
        if {item["id"] for item in snapshot["selected"]} != expected:
            raise ProtocolError("Saved native message batch changed")
        if snapshot["conversation_id"] != run["conversation_id"]:
            raise ProtocolError("Saved native conversation changed")
        self.snapshot = snapshot
        return native_reply_input(snapshot, self.owner)

    def check(self):
        self.runtime.verify_execution_rules()
        access = self.runtime.check_session_binding(self.owner, self.session_id, lease=self.lease)[
            1:
        ]
        if access != self.access:
            raise ProtocolError("Native reply authority changed during execution")

    def _authorize(self, input_id):
        self.check()
        if self.authorized or input_id != self.scope.input_id or self._saved_input() != self.prompt:
            return False
        self.links.require_capacity()
        self.runtime.journal.require_turn_capacity(self.stream_id)
        self.authorized = self.authorize()
        return self.authorized

    def record(self, event):
        self.lease.verify()
        if event["kind"] == "intent":
            request = event["request"]
            method = request["method"]
            if method in {"thread/start", "thread/inject_items", "turn/steer"}:
                raise ProtocolError("Native reply cannot replace history or redirect a turn")
            if method == "turn/start":
                self.check()
                params = request["params"]
                if (
                    not self.authorized
                    or self.request_id is not None
                    or params.get("threadId") != self.scope.native_thread_id
                    or params.get("clientUserMessageId") != self.scope.input_id
                    or params.get("input") != [{"type": "text", "text": self.prompt}]
                    or params.get("outputSchema") != CHOICE_OUTPUT_SCHEMA
                ):
                    raise ProtocolError("Native request differs from the saved reply batch")
                self.links.record(
                    self.scope, self._event("sent", request_id=request["id"]), lease=self.lease
                )
                self.request_id = request["id"]
                event = event | {"reply": self.scope.model_dump(mode="json")}
        self.runtime.journal.record(self.stream_id, event)
        if event["kind"] == "response" and event["id"] == self.request_id:
            turn = event["response"].get("result", {}).get("turn", {})
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and turn_id:
                if self.conversation.turn_id is not None and self.conversation.turn_id != turn_id:
                    raise ProtocolError("Native reply accepted another turn")
                self.links.record(
                    self.scope, self._event("accepted", turn_id=turn_id), lease=self.lease
                )
                self.accepted = turn_id

    def _event(self, kind, **fields):
        return NativeMessageEvent(
            kind=kind,
            stream_id=self.stream_id,
            request_id=fields.pop("request_id", self.request_id),
            **fields,
        )

    def execute(self):
        self.conversation.resume()
        self.conversation.start(
            self.prompt, input_id=self.scope.input_id, output_schema=CHOICE_OUTPUT_SCHEMA
        )
        terminal = self.conversation.wait()
        result = None
        try:
            if terminal == "completed":
                result = final_result(
                    self.runtime.journal,
                    self.stream_id,
                    self.scope,
                    self.accepted,
                    policy=self.output_policy,
                )
            return result
        finally:
            if self.accepted is not None:
                self.links.record(
                    self.scope,
                    self._event(
                        "terminal",
                        turn_id=self.accepted,
                        outcome=terminal,
                        result_digest=result_digest(result) if result is not None else None,
                    ),
                    lease=self.lease,
                )

    def stopped(self, confirmed):
        if self.request_id is not None:
            self.links.record(
                self.scope,
                self._event(
                    "stopped",
                    turn_id=self.accepted,
                    stop_confirmed=confirmed,
                ),
                lease=self.lease,
            )

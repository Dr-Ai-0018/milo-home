"""Serial internal input attempts; the caller owns process cleanup and outcomes.

Used by WorkLauncher.run, never HTTP. A terminal turn is not a completed task.
"""

from .codex_app_server import MainConversation
from .codex_rpc import ProtocolError
from .execution_permits import ExecutionPermits, active_window, permit_row
from .work_decision_native import WORK_CHOICE_SCHEMA, decision_output
from .work_decisions import latest
from .work_deliverables import deliverable_path, expected_deliverable
from .work_output import extract_output
from .work_prompt import WORK_PROMPT_LIMIT, build_work_prompt
from .work_revisits import input_source
from .work_steer import SameTurnSupplements


class WorkDelivery:
    def __init__(
        self,
        runtime,
        owner,
        intent,
        *,
        lease,
        rpc,
        output_policy=None,
        steer_supported=False,
        directories=None,
    ):
        rpc.assert_writer()
        if rpc.sequence or rpc.pending is not None or rpc.broken or rpc.closed:
            raise ProtocolError("A fresh private protocol connection is required")
        runtime.checkpoint(owner, intent, lease=lease)
        self.runtime = runtime
        self.owner = owner
        self.intent = intent
        self.lease = lease
        self.rpc = rpc
        self.output_policy = output_policy
        self.decisions = runtime.native_policy is not None
        self.choice = None
        self.directories = directories
        self.permits = ExecutionPermits(runtime.database, runtime.clock)
        self.input_id = None
        self.prompt = None
        self.permit = None
        self.attempted = False
        self.launched = False
        self.request_id = None
        self.acknowledged = False
        self.finished = False
        self.process_checkpoint = rpc.checkpoint
        rpc.checkpoint = self._checkpoint
        rpc.record = self._record
        self.conversation = MainConversation(
            rpc,
            runtime.conversation_binding(),
            authorize_turn=self._authorize,
            input_limit=WORK_PROMPT_LIMIT,
            execution_policy=runtime.execution_policy,
        )
        self.supplements = SameTurnSupplements(
            runtime, owner, intent, lease, self.conversation, enabled=steer_supported
        )

    def resume(self):
        self.conversation.resume()

    def _checkpoint(self):
        self.process_checkpoint()
        self.runtime.checkpoint(self.owner, self.intent, lease=self.lease)

    def _prepare(self, input_id):
        self.runtime.checkpoint(self.owner, self.intent, lease=self.lease)
        now = int(self.runtime.clock())
        with self.runtime.database.connect() as connection:
            connection.execute("BEGIN")
            window = active_window(connection, self.owner, self.intent.window_id, now)
            task = connection.execute(
                "SELECT * FROM work_tasks WHERE id=?", (self.intent.task_id,)
            ).fetchone()
            input_ = connection.execute(
                "SELECT * FROM work_inputs WHERE id=? AND task_id=?",
                (input_id, self.intent.task_id),
            ).fetchone()
            if input_ is None or input_["delivery"] != "queued":
                raise ProtocolError("Saved input is not available for delivery")
            name = connection.execute(
                "SELECT display_name FROM users WHERE id=?", (self.owner,)
            ).fetchone()[0]
            expected = expected_deliverable(connection, self.intent.task_id)
            self.prompt = build_work_prompt(
                actor_id=self.owner,
                display_name=name,
                task=task,
                input_=input_,
                window=window,
                observed_at=now,
                decisions=self.decisions,
                previous_decision=latest(connection, task["id"]),
                revisit=input_source(connection, self.intent.window_id, input_id),
                directories=self.directories,
                deliverable=deliverable_path(task["id"], expected["filename"])
                if expected
                else None,
            )
        self.runtime._identity(self.lease)
        self.runtime.journal.require_turn_capacity(self.intent.stream_id)
        self.permit = self.permits.reserve(self.owner, self.intent.window_id, input_id)
        self.input_id = input_id

    def _authorize(self, input_id):
        if self.launched or self.permit is None or input_id != self.input_id:
            return False
        self.runtime.checkpoint(self.owner, self.intent, lease=self.lease)
        self.runtime.journal.require_turn_capacity(self.intent.stream_id)
        self.launched = self.permits.consume(
            self.owner,
            self.permit["id"],
            input_id,
            claim_input=True,
            freeze_reception=self.decisions,
        )
        self.lease.verify()
        return self.launched

    def _record(self, event):
        self.runtime._identity(self.lease)
        if event["kind"] == "intent":
            request = event["request"]
            method = request["method"]
            if method == "thread/start":
                raise ProtocolError("Work delivery does not support this method")
            if method == "turn/steer":
                event = self.supplements.record_intent(event)
            if method == "turn/start":
                params = request["params"]
                if (
                    not self.launched
                    or self.request_id is not None
                    or params.get("threadId") != self.conversation.binding.thread_id
                    or params.get("clientUserMessageId") != self.input_id
                    or params.get("input") != [{"type": "text", "text": self.prompt}]
                    or params.get("outputSchema")
                    != (WORK_CHOICE_SCHEMA if self.decisions else None)
                ):
                    raise ProtocolError("Work input does not match its reserved request")
                event = event | {
                    "work": {
                        "input_id": self.input_id,
                        "permit_id": self.permit["id"],
                        "window_id": self.intent.window_id,
                        "task_id": self.intent.task_id,
                        "stream_id": self.intent.stream_id,
                    }
                }
        self.runtime.journal.record(self.intent.stream_id, event)
        if event["kind"] == "intent" and event["request"]["method"] == "turn/start":
            self.request_id = event["request"]["id"]

    def start(self, input_id) -> str:
        self.rpc.assert_writer()
        if self.attempted and (
            not self.finished or self.conversation.state != "completed" or input_id == self.input_id
        ):
            raise ProtocolError("This input attempt cannot be reused")
        if not self.attempted and self.conversation.state != "idle":
            raise ProtocolError("The main conversation is not ready")
        self.input_id = None
        self.prompt = None
        self.permit = None
        self.launched = False
        self.request_id = None
        self.acknowledged = False
        self.finished = False
        self.choice = None
        self.attempted = True
        try:
            self._prepare(input_id)
            turn_id = self.conversation.start(
                self.prompt,
                input_id=self.input_id,
                output_schema=WORK_CHOICE_SCHEMA if self.decisions else None,
            )
            self._acknowledge()
            return turn_id
        except BaseException:
            self._uncertain_or_cancel()
            raise

    def _acknowledge(self):
        self.runtime._identity(self.lease)
        with self.runtime.database.connect(write=True) as connection:
            permit = permit_row(connection, self.permit["id"])
            if permit["state"] != "launched" or self.request_id is None:
                raise ProtocolError("Input acknowledgement needs reconciliation")
            changed = connection.execute(
                "UPDATE work_inputs SET delivery='acknowledged' WHERE id=? AND delivery='sending'",
                (self.input_id,),
            ).rowcount
            if changed != 1:
                raise ProtocolError("Input acknowledgement needs reconciliation")
            self.supplements.open(connection, self.permit["id"])
        self.acknowledged = True
        self.lease.verify()

    def wait(self) -> str:
        self.rpc.assert_writer()
        if not self.acknowledged or self.finished:
            raise ProtocolError("No acknowledged unfinished work turn")
        try:
            terminal = self.conversation.wait(between_pumps=self.supplements.send_next)
            self.supplements.close()
            self.runtime._identity(self.lease)
            observed_at = int(self.runtime.clock())
            output = self._output(terminal, observed_at)
            self.runtime.journal.record(
                self.intent.stream_id,
                {
                    "kind": "work_terminal",
                    "work": {
                        "input_id": self.input_id,
                        "permit_id": self.permit["id"],
                        "window_id": self.intent.window_id,
                        "task_id": self.intent.task_id,
                        "stream_id": self.intent.stream_id,
                    },
                    "request_id": self.request_id,
                    "turn_id": self.conversation.turn_id,
                    "outcome": terminal,
                    "observed_at": observed_at,
                    **({"output": output} if output is not None else {}),
                    **({"decision": self.choice.model_dump()} if self.choice is not None else {}),
                },
            )
            if not self.permits.finish(self.permit["id"], delivery_known=True):
                raise ProtocolError("Turn completion needs reconciliation")
            self.finished = True
            self.lease.verify()
            return terminal
        except BaseException:
            self._uncertain_or_cancel()
            raise

    def _output(self, terminal, observed_at):
        if self.output_policy is None:
            return None
        snapshot = self.runtime.journal.work_snapshot(self.intent.stream_id)
        if self.decisions and terminal == "completed":
            self.choice, output = decision_output(
                snapshot["entries"],
                thread_id=self.conversation.binding.thread_id,
                turn_id=self.conversation.turn_id,
                observed_at=observed_at,
                policy=self.output_policy,
            )
            return output
        return extract_output(
            snapshot["entries"],
            thread_id=self.conversation.binding.thread_id,
            turn_id=self.conversation.turn_id,
            policy=self.output_policy,
        )

    def _uncertain_or_cancel(self):
        if self.permit is None:
            return
        self.runtime._identity(self.lease)
        if not self.launched:
            self.permits.cancel(self.permit["id"])
            return
        self.permits.finish(self.permit["id"], delivery_known=False)
        with self.runtime.database.connect(write=True) as connection:
            # A durable acknowledgement stays true even if the later outcome is unknown.
            connection.execute(
                "UPDATE work_inputs SET delivery='unknown' WHERE id=? AND delivery='sending'",
                (self.input_id,),
            )
        self.lease.verify()

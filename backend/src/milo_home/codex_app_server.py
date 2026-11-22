"""Single-writer main-thread adapter; no HTTP, automatic retries or implicit threads."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .codex_execution_policy import NativeExecutionPolicy
from .codex_rpc import DeliveryUnknown, ProtocolError, RequestRejected, StdioRpc

BENIGN = {
    "thread/started",
    "thread/status/changed",
    "thread/tokenUsage/updated",
    "account/rateLimits/updated",
    "remoteControl/status/changed",
    "warning",
    "configWarning",
    "guardianWarning",
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/textDelta",
    "item/reasoning/summaryPartAdded",
    "item/commandExecution/outputDelta",
    "item/commandExecution/terminalInteraction",
    "item/fileChange/outputDelta",
    "turn/diff/updated",
    "turn/plan/updated",
    "thread/compacted",
}
AUTO_REVIEW_NOTICES = {
    "item/autoApprovalReview/started",
    "item/autoApprovalReview/completed",
    "autoApprovalReview/strictReviewRequired",
}
ITEMS = {
    "userMessage",
    "agentMessage",
    "reasoning",
    "commandExecution",
    "fileChange",
    "contextCompaction",
}
TERMINAL = {"completed", "failed", "interrupted"}


@dataclass(frozen=True)
class ThreadBinding:
    thread_id: str
    provider: str
    model: str
    cwd: Path

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.thread_id):
            raise ValueError("Invalid stored thread identity")
        if not self.provider or not self.model or not self.cwd.is_absolute():
            raise ValueError("Incomplete stored thread binding")


class MainConversation:
    def __init__(
        self,
        rpc: StdioRpc,
        binding: ThreadBinding,
        *,
        authorize_turn: Callable,
        input_limit=4000,
        execution_policy=NativeExecutionPolicy.LEGACY_DIRECT,
    ):
        if type(input_limit) is not int or not 1 <= input_limit <= 65536:
            raise ValueError("Invalid protocol text limit")
        self.rpc = rpc
        self.binding = binding
        self.authorize_turn = authorize_turn
        self.input_limit = input_limit
        if not isinstance(execution_policy, NativeExecutionPolicy):
            raise ValueError("An explicit native execution policy is required")
        self.execution_policy = execution_policy
        self.state = "unbound"
        self.turn_id = None
        self.starting = False
        self.terminal = None
        self.final_error_reported = False
        rpc.on_notification = self._notification

    @property
    def accepts_supplements(self):
        return self.state == "running" and not self.final_error_reported

    def resume(self):
        self.rpc.assert_writer()
        if self.state != "unbound":
            raise ProtocolError("Conversation already bound")
        try:
            self._resume()
        except Exception:
            self.state = "needs_reconciliation"
            raise

    def _resume(self):
        self.rpc.request(
            "initialize",
            {
                "clientInfo": {"name": "milo_home", "version": "0.1.0"},
                "capabilities": {"experimentalApi": False},
            },
        )
        self.rpc.notify_initialized()
        result = self.rpc.request(
            "thread/resume",
            {
                "threadId": self.binding.thread_id,
                "model": self.binding.model,
                "excludeTurns": True,
                "cwd": str(self.binding.cwd),
                **self.execution_policy.resume_parameters(),
            },
        )
        self._verify_binding(result)
        if result["thread"].get("status", {}).get("type") != "idle":
            self.state = "needs_reconciliation"
            raise ProtocolError("Stored thread is not idle")
        self.state = "idle"

    def _verify_binding(self, result):
        valid = (
            result.get("thread", {}).get("id") == self.binding.thread_id
            and result.get("modelProvider") == self.binding.provider
            and result.get("model") == self.binding.model
            and result.get("cwd") == str(self.binding.cwd)
            and self.execution_policy.matches(result)
        )
        if not valid:
            self.state = "needs_reconciliation"
            raise ProtocolError("Effective thread configuration mismatch")

    def start(self, text: str, *, input_id: str, output_schema: dict | None = None) -> str:
        self.rpc.assert_writer()
        if self.state not in {"idle", "completed", "failed"} or self.rpc.broken:
            raise ProtocolError("Conversation cannot start another turn")
        self._validate_input(text, input_id, self.input_limit)
        if output_schema is not None and not isinstance(output_schema, dict):
            raise ValueError("Native output schema must be an object")
        # Upper layer must durably reserve a fresh launch permit for every turn.
        if not self.authorize_turn(input_id):
            raise ProtocolError("Turn launch not authorized")
        self.state = "starting"
        self.starting = True
        self.turn_id = None
        self.terminal = None
        self.final_error_reported = False
        try:
            result = self.rpc.request(
                "turn/start",
                {
                    "threadId": self.binding.thread_id,
                    "clientUserMessageId": input_id,
                    "input": [{"type": "text", "text": text}],
                    **({"outputSchema": output_schema} if output_schema is not None else {}),
                },
            )
            turn = result.get("turn", {})
            turn_id = turn.get("id")
            if (
                not isinstance(turn_id, str)
                or not turn_id
                or (self.turn_id is not None and turn_id != self.turn_id)
            ):
                raise ProtocolError("Turn identity mismatch")
            self.turn_id = turn_id
            if turn.get("status") not in TERMINAL | {"inProgress"}:
                raise ProtocolError("Unknown turn status")
            if turn.get("status") in TERMINAL:
                self._terminal(turn)
            elif self.terminal is None:
                self.state = "running"
            return turn_id
        except RequestRejected:
            self.state = "needs_reconciliation" if self.turn_id else "idle"
            raise
        except Exception:
            self.state = "needs_reconciliation"
            raise
        finally:
            self.starting = False

    def steer(self, text: str, *, input_id: str, expected_turn: str):
        self.rpc.assert_writer()
        self._validate_input(text, input_id, self.input_limit)
        if not self.accepts_supplements or expected_turn != self.turn_id:
            raise ProtocolError("Input turn changed")
        try:
            result = self.rpc.request(
                "turn/steer",
                {
                    "threadId": self.binding.thread_id,
                    "expectedTurnId": expected_turn,
                    "clientUserMessageId": input_id,
                    "input": [{"type": "text", "text": text}],
                },
            )
            if result.get("turnId") != expected_turn:
                raise DeliveryUnknown("Supplement response does not match the active turn")
        except DeliveryUnknown:
            self.state = "needs_reconciliation"
            raise
        # Accepted input is not proof that it changed the answer.

    def wait(self, *, between_pumps=None) -> str:
        self.rpc.assert_writer()
        if self.turn_id is None or self.rpc.broken:
            raise ProtocolError("No active turn")
        try:
            while self.terminal is None:
                self.rpc.pump()
                if self.terminal is None and between_pumps is not None:
                    between_pumps()
        except Exception:
            self.state = "needs_reconciliation"
            raise
        return self.terminal

    def stop(self) -> bool:
        self.rpc.assert_writer()
        self.state = "stopping"
        try:
            if self.turn_id and not self.rpc.broken:
                self.rpc.request(
                    "turn/interrupt",
                    {
                        "threadId": self.binding.thread_id,
                        "turnId": self.turn_id,
                    },
                    timeout=2,
                )
        except (ProtocolError, DeliveryUnknown, RequestRejected):
            pass
        finally:
            self.state = "needs_reconciliation"
            ended = self.rpc.close()
            self.state = "stopped" if ended else "needs_reconciliation"
        return ended

    def _notification(self, message: dict):
        method = message["method"]
        params = message.get("params", {})
        if params.get("threadId", self.binding.thread_id) != self.binding.thread_id:
            raise ProtocolError("Unexpected thread event")
        if method in BENIGN:
            return
        if method in AUTO_REVIEW_NOTICES:
            if (
                self.execution_policy is not NativeExecutionPolicy.AUTO_REVIEW
                or params.get("threadId") != self.binding.thread_id
                or self.turn_id is None
                or params.get("turnId") != self.turn_id
            ):
                raise ProtocolError("Unexpected native approval review event")
            # These are observations of Codex decisions, never permission grants.
            return
        if method == "error":
            self._turn_error(params)
            return
        if method in {"item/started", "item/completed"}:
            if params.get("item", {}).get("type") not in ITEMS:
                raise ProtocolError("Unsupported execution item")
            return
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn", {})
            turn_id = turn.get("id")
            if not isinstance(turn_id, str) or not turn_id:
                raise ProtocolError("Missing turn identity")
            if self.starting and self.turn_id is None:
                self.turn_id = turn_id
            if turn_id != self.turn_id:
                raise ProtocolError("Unexpected turn event")
            if method == "turn/completed":
                self._terminal(turn)
            return
        raise ProtocolError("Unsupported protocol event")

    def _turn_error(self, params):
        turn_id = params.get("turnId")
        if (
            params.get("threadId") != self.binding.thread_id
            or not isinstance(turn_id, str)
            or not turn_id
            or not isinstance(params.get("error"), dict)
            or type(params.get("willRetry")) is not bool
            or (self.turn_id is not None and turn_id != self.turn_id)
            or self.state not in {"starting", "running", "stopping"}
            or (self.turn_id is None and not self.starting)
        ):
            raise ProtocolError("Unexpected turn error")
        if self.turn_id is None:
            self.turn_id = turn_id
        if not params["willRetry"]:
            self.final_error_reported = True
        # Only the matching terminal response or event can settle this turn.

    def _terminal(self, turn: dict):
        status = turn.get("status")
        if status not in TERMINAL or (self.terminal is not None and self.terminal != status):
            raise ProtocolError("Invalid terminal event")
        self.terminal = status
        self.state = "stopping" if status == "interrupted" else status

    @staticmethod
    def _validate_input(text: str, input_id: str, limit=4000):
        if not isinstance(text, str) or not text.strip() or len(text) > limit:
            raise ValueError("Invalid work input")
        if not isinstance(input_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", input_id):
            raise ValueError("Invalid input identity")

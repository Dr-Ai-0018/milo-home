"""Bound one ordinary reply to its saved run identity and verified native process."""

import os
import time

from .codex_config import private_read
from .codex_process import DirectUnit
from .codex_rpc import ProtocolError, StdioRpc
from .errors import ApiError
from .execution_pulse import PULSE_INTERVAL
from .native_reply_delivery import NativeReplyDelivery
from .native_reply_route import save_native_route
from .work_output import OutputPolicy
from .work_stop import boot_id
from .worker import validate_unit


class NativeReplyProcess:
    def __init__(self, worker, run_id, ticket, *, lease):
        self.worker = worker
        self.runtime = worker.native_runtime
        self.store = worker.store
        self.owner = worker.owner
        self.run_id = run_id
        self.ticket = ticket
        self.lease = lease
        self.unit = None
        self.rpc = None
        self.delivery = None
        self.stream_id = None
        self.environment_file = None
        self.environment_created = False
        self.pulse = None
        self.next_pulse = 0
        self.failure_code = "configuration_error"
        self.closed = None
        self.access = None

    def checkpoint(self):
        run = self.store.checkpoint(self.run_id)
        self.runtime.verify_execution_rules()
        linked = self.runtime.journal.business_binding()
        if linked is None:
            raise ProtocolError("Native main business binding is missing")
        checked = self.runtime.check_session_binding(
            self.owner, linked.session_id, lease=self.lease
        )
        if self.access is not None and self.access != checked:
            raise ProtocolError("Native reply authority changed during execution")
        self.access = checked
        if run["stop_reason"] or run["status"] not in {"queued", "running"}:
            raise ApiError(409, "reply_stop_requested", "本轮回信已经请求停止。")
        if self.unit is not None:
            self.unit.checkpoint()
        now = time.monotonic()
        if self.pulse and now >= self.next_pulse:
            if not self.store.heartbeat(self.run_id, self.pulse):
                raise ProtocolError("Native reply heartbeat is no longer valid")
            self.next_pulse = now + PULSE_INTERVAL
        return run

    def _authorize(self):
        self.checkpoint()
        if self.pulse is not None or not self.unit.ready():
            return False
        if not self.worker.dispatch.authorize_launch(self.ticket):
            return False
        if not self.store.authorize_launch(self.run_id):
            return False
        self.pulse = self.store.started(self.run_id)
        if not self.pulse:
            return False
        self.failure_code = "execution_failed"
        self.next_pulse = time.monotonic() + PULSE_INTERVAL
        self.checkpoint()
        return True

    def _record_process(self, phase, **fields):
        self.runtime.journal.record(
            self.stream_id,
            {
                "kind": "process",
                "unit": self.unit.name,
                "phase": phase,
                **fields,
            },
        )

    def execute(self):
        try:
            self._start()
            return self.delivery.execute()
        finally:
            if not self.close():
                self.store.stop(self.run_id, "interrupted")
                raise ApiError(
                    503, "needs_reconciliation", "原生投递或执行组停止尚未核对，占用继续保留。"
                )

    def _start(self):
        run = self.checkpoint()
        journal = self.runtime.journal
        installed = journal.installation()
        binding = journal.binding()
        journal.bind(binding, self.worker.config, installed)
        command = installed.command(workspace=binding.cwd)
        environment = installed.environment(
            journal.path.parent, self.worker.config, self.worker.credential
        )
        policy = OutputPolicy.for_runtime(
            self.worker.config, self.worker.credential, journal.path.parent
        )
        self.unit = DirectUnit.from_reservation(
            unit_name=validate_unit(run) + ".service",
            created_at=run["created_at"],
            deadline_at=run["deadline_at"],
        )
        if journal.outstanding():
            raise ProtocolError("Previous native execution still needs reconciliation")
        save_native_route(self.worker._base(self.run_id), self.runtime, self.owner, self.run_id)
        self.stream_id = journal.open_stream(self.unit.name)
        self.environment_file = journal.path.parent / (self.stream_id + ".env")
        self._write_environment(environment)
        while time.monotonic() < self.unit.next_cpu:
            self.checkpoint()
            time.sleep(0.02)
        self.checkpoint()
        self._record_process(
            "launch_intent",
            native_policy=self.runtime.native_policy.summary(),
            reply_run_id=self.run_id,
        )
        if private_read(journal.path.parent / "config.toml") != self.worker.config.content:
            raise ProtocolError("Native runtime configuration changed before launch")
        process = self.unit.launch(command, environment_file=self.environment_file)
        self.unit.wait_ready()
        self.checkpoint()
        self._record_process(
            "ready",
            invocation=self.unit.invocation,
            main_pid=self.unit.main_pid,
            group="/" + str(self.unit.group.relative_to(self.unit.cgroup_root)),
            boot_id=boot_id(),
        )
        self.rpc = StdioRpc(
            process,
            deadline=self.unit.deadline,
            record=journal.recorder(self.stream_id),
            stop=self.unit.stop,
            checkpoint=self.checkpoint,
        )
        self.delivery = NativeReplyDelivery(
            self.runtime,
            self.owner,
            self.run_id,
            self.store,
            self.rpc,
            self.stream_id,
            lease=self.lease,
            authorize=self._authorize,
            output_policy=policy,
        )

    def _write_environment(self, content):
        descriptor = os.open(
            self.environment_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        self.environment_created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    def close(self):
        if self.closed is not None:
            return self.closed
        confirmed = self.unit is None or not self.unit.launch_attempted
        evidence = True
        try:
            if not confirmed:
                confirmed = (
                    self.rpc.close() is True if self.rpc is not None else self.unit.stop() is True
                )
                if self.rpc is None and self.unit.process is not None:
                    for pipe in (
                        self.unit.process.stdin,
                        self.unit.process.stdout,
                        self.unit.process.stderr,
                    ):
                        pipe.close()
        except BaseException:
            confirmed = False
        try:
            if self.delivery is not None:
                self.delivery.stopped(confirmed)
            if self.stream_id is not None:
                self._record_process("stop_confirmed" if confirmed else "stop_unknown")
        except BaseException:
            evidence = False
        try:
            journal_closed = self.stream_id is None or self.runtime.journal.close_stream(
                self.stream_id, stop=lambda: confirmed
            )
        except BaseException:
            journal_closed = False
        try:
            if self.environment_created:
                self.environment_file.unlink(missing_ok=True)
        except OSError:
            evidence = False
        self.closed = confirmed and evidence and journal_closed
        return self.closed

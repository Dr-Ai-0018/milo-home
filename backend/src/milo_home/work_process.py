"""One claimed host process, held under the common lease until bounded cleanup.

The preparation context refuses turns. The explicit internal run path connects
saved input delivery; neither path enables HTTP or a background service.
"""

import os
import time
from contextlib import contextmanager
from uuid import uuid4

from .codex_app_server import MainConversation
from .codex_config import CodexRuntime, NewawzConfig, private_directory, private_read
from .codex_process import DirectUnit
from .codex_rpc import ProtocolError, StdioRpc
from .codex_steer_capability import supports_turn_steer
from .errors import ApiError
from .execution_control import ExecutionControl
from .execution_lock import execution_lock
from .work_attachment_runtime import collect_attachments, prepare_directories
from .work_coordinator import WorkResult, run_inputs
from .work_deliverables import collect_deliverable
from .work_delivery import WorkDelivery
from .work_output import OutputPolicy
from .work_recovery import WorkRecovery
from .work_runtime import WorkRuntime
from .work_stop import boot_id
from .work_tasks import WorkTasks


class WorkLauncher:
    def __init__(
        self,
        runtime: WorkRuntime,
        config: NewawzConfig,
        installed: CodexRuntime,
        *,
        stop_requested=lambda: False,
        attachment_store=None,
    ):
        self.runtime = runtime
        self.config = config
        self.installed = installed
        self.stop_requested = stop_requested
        self.attachment_store = attachment_store

    @contextmanager
    def open(self, owner: str, window_id: str, *, credential: str):
        with self._open(owner, window_id, credential=credential, execute=False) as live:
            yield live.conversation

    def run(self, owner: str, window_id: str, *, credential: str) -> WorkResult:
        with execution_lock(self.runtime.database) as lease:
            prepared = self._prepare(credential, execute=True)
            with self._open_prepared(owner, window_id, lease, prepared, execute=True) as live:
                result = self._run_live(live)
            self._collect(live, prepared[2])
        # Publishing cannot turn completed file operations into another task
        # attempt. A failure here leaves the settled outcome and private receipt.
        WorkRecovery(self.runtime).replay_results(owner, window_id)
        return WorkResult(live.outcome, result.turns)

    def _collect(self, live, policy):
        choice = getattr(live.delivery, "choice", None)
        if choice is not None and choice.decision != "accepted":
            return
        if live.outcome == "completed":
            collect_deliverable(
                self.runtime.database,
                live.owner,
                live.intent.task_id,
                lease=live.lease,
                clock=self.runtime.clock,
                policy=policy,
            )
            collect_attachments(live, policy, self.attachment_store)

    @staticmethod
    def _run_live(live):
        result = run_inputs(
            live.runtime, live.owner, live.intent, lease=live.lease, delivery=live.delivery
        )
        live.outcome = result.outcome
        return result

    def _prepare(self, credential, *, execute):
        self.runtime.verify_execution_rules()
        journal = self.runtime.journal
        binding = journal.binding()
        command = self.installed.command(workspace=binding.cwd)
        journal.bind(binding, self.config, self.installed)
        environment = self.installed.environment(journal.path.parent, self.config, credential)
        policy = OutputPolicy.for_runtime(self.config, credential, journal.path.parent)
        return command, environment, policy, execute and supports_turn_steer(self.installed.cli)

    def run_task(self, owner, task_id, seconds, *, credential):
        """Reserve and execute one fresh saved task under the same host lease."""
        with execution_lock(self.runtime.database) as lease:
            window_id, result = self._run_task_locked(
                owner, task_id, seconds, credential=credential, lease=lease
            )
        WorkRecovery(self.runtime).replay_results(owner, window_id)
        return window_id, result

    def _run_task_locked(self, owner, task_id, seconds, *, credential, lease, limit=None):
        self.runtime.check_task_binding(owner, task_id, lease=lease)
        prepared = self._prepare(credential, execute=True)
        if self.runtime.journal.outstanding():
            raise ProtocolError("Previous private execution still needs reconciliation")
        self.runtime.journal.require_capacity()
        if self.stop_requested():
            raise ApiError(409, "work_stop_requested", "本次工作启动已取消。")
        if limit is not None:
            seconds = limit.seconds()
        control = ExecutionControl(self.runtime.database, self.runtime.clock)
        window = control.reserve(
            owner, task_id, seconds, decisions=self.runtime.native_policy is not None
        )
        try:
            with self._open_prepared(
                owner, window["id"], lease, prepared, execute=True, limit=limit
            ) as live:
                result = self._run_live(live)
        except BaseException:
            self._retire_unclaimed(owner, task_id, window["id"], lease, control)
            raise
        self._collect(live, prepared[2])
        return window["id"], WorkResult(live.outcome, result.turns)

    def _retire_unclaimed(self, owner, task_id, window_id, lease, control):
        # A durable intention may have spawned even when no private stream is
        # present. Only absence in the original database proves no launch here.
        self.runtime._identity(lease)
        with self.runtime.database.connect() as connection:
            claimed = connection.execute(
                "SELECT 1 FROM execution_start_intents WHERE window_id=?", (window_id,)
            ).fetchone()
        if claimed is None:
            if self.stop_requested():
                try:
                    WorkTasks(self.runtime.database, self.runtime.clock).stop(
                        owner, task_id, str(uuid4())
                    )
                except ApiError:
                    pass
            control.close(window_id, stop=lambda: True, delivery_known=True, outcome="failed")
        self.runtime._identity(lease)

    @contextmanager
    def _open(self, owner, window_id, *, credential, execute):
        """Consume a fresh startup claim; never accept a historical StartupIntent."""
        with execution_lock(self.runtime.database) as lease:
            prepared = self._prepare(credential, execute=execute)
            with self._open_prepared(owner, window_id, lease, prepared, execute=execute) as live:
                yield live

    @contextmanager
    def _open_prepared(self, owner, window_id, lease, prepared, *, execute, limit=None):
        command, environment, policy, steer_supported = prepared
        intent = self.runtime.claim_start(owner, window_id, lease=lease)
        live = _WorkProcess(self.runtime, owner, intent, lease, self.stop_requested)
        live.limit = limit
        try:
            if execute:
                prepare_directories(live, self.attachment_store)
            live.start(
                command,
                environment,
                self.config,
                execute=execute,
                output_policy=policy,
                steer_supported=steer_supported,
            )
            yield live
        except BaseException:
            live.uncertain = live.unit is not None and live.unit.launch_attempted
            live.outcome = "failed"
            live.close()
            raise
        else:
            if not live.close():
                raise ProtocolError("Work process requires reconciliation")


class _WorkProcess:
    def __init__(self, runtime, owner, intent, lease, stop_requested=lambda: False):
        self.runtime = runtime
        self.owner = owner
        self.intent = intent
        self.lease = lease
        self.stop_requested = stop_requested
        self.stop_key = str(uuid4())
        self.unit = None
        self.rpc = None
        self.conversation = None
        self.delivery = None
        self.uncertain = False
        self.outcome = "cancelled"
        self.closed = None
        self.environment_file = runtime.journal.path.parent / (intent.stream_id + ".env")
        self.environment_created = False
        self.limit = None
        self.directories = None
        self.attachments_enrolled = False

    def checkpoint(self):
        self._signal_checkpoint()
        self.runtime.verify_execution_rules()
        self.runtime.checkpoint(self.owner, self.intent, lease=self.lease)
        if self.unit is not None:
            self.unit.checkpoint()

    def _record_stop(self):
        if self.stop_requested():
            self.runtime._identity(self.lease)
            WorkTasks(self.runtime.database, self.runtime.clock).stop(
                self.owner, self.intent.task_id, self.stop_key
            )
            return True
        return False

    def _signal_checkpoint(self):
        if self._record_stop():
            raise ApiError(409, "work_stop_requested", "已请求停止本次工作。")

    def _process_checkpoint(self):
        self._signal_checkpoint()
        self.runtime.verify_execution_rules()
        self.unit.checkpoint()

    def _record_process(self, phase, **evidence):
        self.runtime.journal.record(
            self.intent.stream_id,
            {"kind": "process", "unit": self.intent.unit_name, "phase": phase, **evidence},
        )

    def start(self, command, environment, config, *, execute, output_policy, steer_supported):
        intent = self.intent
        self.unit = DirectUnit.from_reservation(
            unit_name=intent.unit_name,
            created_at=intent.created_at,
            deadline_at=intent.deadline_at,
        )
        if self.limit is not None:
            self.unit.deadline = min(self.unit.deadline, self.limit.monotonic_deadline)
        self.checkpoint()
        self._write_environment(environment)
        # Wait for a full initial CPU sample before the first spawn. Continue
        # checking stop/authorization/deadline while gathering that sample.
        sample_until = self.unit.next_cpu
        while time.monotonic() < sample_until:
            self.checkpoint()
            time.sleep(0.02)
        self.checkpoint()
        self._record_process(
            "launch_intent",
            **(
                {"native_policy": self.runtime.native_policy.summary()}
                if self.runtime.native_policy is not None
                else {}
            ),
        )
        # Recheck after preparation and durable evidence, immediately before spawn.
        if private_read(self.runtime.journal.path.parent / "config.toml") != config.content:
            raise ProtocolError("Runtime configuration changed before launch")
        self.checkpoint()
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
            record=self.runtime.journal.recorder(intent.stream_id),
            stop=self.unit.stop,
            checkpoint=self._process_checkpoint if execute else self.checkpoint,
        )
        if execute:
            self.delivery = WorkDelivery(
                self.runtime,
                self.owner,
                intent,
                lease=self.lease,
                rpc=self.rpc,
                output_policy=output_policy,
                steer_supported=steer_supported,
                directories=self.directories,
            )
            self.conversation = self.delivery.conversation
        else:
            self.conversation = MainConversation(
                self.rpc,
                self.runtime.conversation_binding(),
                authorize_turn=lambda _: False,
                execution_policy=self.runtime.execution_policy,
            )
        self.conversation.resume()
        self.checkpoint()
        if not self.unit.ready():
            raise ProtocolError("Execution identity changed during resume")

    def _write_environment(self, environment):
        private_directory(self.environment_file.parent)
        descriptor = os.open(
            self.environment_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        # Track ownership immediately after exclusive creation, including failed
        # writes/fsync. A preexisting file never reaches this point.
        self.environment_created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(environment)
            stream.flush()
            os.fsync(stream.fileno())

    def _stop(self):
        if self.unit is None or not self.unit.launch_attempted:
            return True
        if self.rpc is not None:
            return self.rpc.close() is True
        confirmed = self.unit.stop() is True
        process = self.unit.process
        if process is not None:
            for pipe in [process.stdin, process.stdout, process.stderr]:
                pipe.close()
        return confirmed

    def close(self) -> bool:
        if self.closed is not None:
            return self.closed
        self.closed = False
        confirmed = False
        journal_closed = False
        try:
            confirmed = self._stop()
        except BaseException:
            self.uncertain = True
        try:
            self._record_process("stop_confirmed" if confirmed else "stop_unknown")
            journal_closed = self.runtime.journal.close_stream(
                self.intent.stream_id, stop=lambda: confirmed
            )
        except BaseException:
            # Stop still ran. Missing durable evidence cannot free business occupancy.
            self.uncertain = True
        try:
            self.runtime._identity(self.lease)
            try:
                self._record_stop()
            except ApiError:
                # Revocation can prevent recording a user's stop. It must not
                # skip authoritative process/occupancy cleanup below.
                pass
            control = ExecutionControl(self.runtime.database, self.runtime.clock)
            self.closed = control.close(
                self.intent.window_id,
                stop=lambda: confirmed,
                delivery_known=journal_closed and not self.uncertain,
                outcome=self.outcome,
            )
            self.lease.verify()
            if self.closed:
                with self.runtime.database.connect() as connection:
                    self.outcome = connection.execute(
                        "SELECT status FROM work_tasks WHERE id=?", (self.intent.task_id,)
                    ).fetchone()[0]
                self.lease.verify()
        finally:
            # Only remove the exact ephemeral file this object created.
            if self.environment_created:
                self.environment_file.unlink(missing_ok=True)
        return self.closed

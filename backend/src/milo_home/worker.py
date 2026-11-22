import fcntl
import os
import signal
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from .automatic_budget import AutomaticPolicy
from .dispatch import DispatchStore, DispatchTicket
from .errors import ApiError
from .execution_lock import execution_lock
from .native_reply_route import has_native_route
from .reply_result import ReplyResult
from .run_environment import codex_arguments, prepare, unit_command
from .run_transport import execute, stop_unit
from .runs import RunStore


@contextmanager
def worker_lock(database, runtime: Path | None = None):
    with execution_lock(database) as lease, _legacy_lock(database, runtime):
        yield lease


@contextmanager
def _legacy_lock(database, runtime):
    path = (runtime or database.path.parent) / "worker.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
        ):
            raise ValueError("Worker lock must be a private caller-owned ordinary file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ApiError(409, "worker_busy", "读信进程正在运行。") from error
        yield
    finally:
        os.close(descriptor)


def validate_unit(run: dict) -> str:
    expected = "milo-reply-" + str(UUID(run["id"]))
    if run["unit_name"] != expected:
        raise ValueError("Run unit identity does not match")
    return expected


class Worker:
    def __init__(
        self,
        database,
        profile: Path | None = None,
        clock=time.time,
        *,
        runtime: Path | None = None,
        policy: AutomaticPolicy | None = None,
        stop_requested=None,
        progress=None,
        scope=None,
    ):
        self.database = database
        self.store = RunStore(database, clock, stop_requested=stop_requested, progress=progress)
        self.dispatch = DispatchStore(database, clock, policy=policy, scope=scope)
        self.profile = profile
        self.runtime = runtime

    def _prepare_run(self, run_id):
        return prepare(self._base(run_id), self.store.snapshot(run_id), self.profile)

    def _publish_result(self, run_id, result, code, events, ended):
        return self.store.publish(run_id, result)

    def recover(self) -> str | None:
        with worker_lock(self.database, self.runtime) as lease:
            lease.verify()
            run = self.store.active()
            if not run:
                self.dispatch.reconcile()
                return None
            self._recover_locked(run)
            self.dispatch.reconcile()
            return run["id"]

    def _recover_locked(self, run):
        if has_native_route(self._base(run["id"])):
            raise ApiError(
                409, "native_reply_recovery_required", "本轮属于原生主对话，请走独立回信恢复入口。"
            )
        unit = validate_unit(run)
        reason = "timed_out" if int(self.store.clock()) >= run["deadline_at"] else "interrupted"
        self.store.stop(run["id"], reason)
        if not stop_unit(unit):
            raise ApiError(503, "termination_unknown", "尚未确认旧进程结束，占用继续保留。")
        self.store.finish_failed(run["id"], "interrupted")
        self._remove_credential(run["id"])

    def _base(self, run_id: str) -> Path:
        return (self.runtime or self.database.path.parent / "runs") / str(UUID(run_id))

    def _remove_credential(self, run_id: str):
        (self._base(run_id) / "credential.env").unlink(missing_ok=True)

    def wake(self, thread_id: str, timeout: int = 3600) -> dict:
        with worker_lock(self.database, self.runtime) as lease:
            run_id = self.store.queue(str(UUID(thread_id)), timeout)
            return self._execute(run_id, lease=lease)

    def wake_automatic(self, timeout: int = 3600) -> dict | None:
        """Internal single iteration; no scheduler or paid CLI entry is installed."""
        with worker_lock(self.database, self.runtime) as lease:
            lease.verify()
            active = self.store.active()
            if active:
                if not self.dispatch.is_automatic(active["id"]):
                    return None
                self._recover_locked(active)
            self.dispatch.reconcile()
            ticket = self.dispatch.reserve(timeout)
            if not ticket:
                return None
            try:
                return self._execute(ticket.run_id, ticket, lease=lease)
            finally:
                lease.verify()
                self.dispatch.reconcile()

    def _execute(self, run_id: str, ticket: DispatchTicket | None = None, *, lease) -> dict:
        unit = validate_unit(self.store.get(run_id))
        failure_code = "configuration_error"
        try:
            run = self.store.checkpoint(run_id)
            if run["stop_reason"]:
                failure_code = "interrupted"
            else:
                root = self._prepare_run(run_id)
                lease.verify()
                failure_code = "interrupted"
                run = self.store.checkpoint(run_id)
                remaining = run["deadline_at"] - int(self.store.clock())
                if remaining <= 0:
                    self.store.stop(run_id, "timed_out")
                elif (
                    not run["stop_reason"]
                    and (ticket is None or self.dispatch.authorize_launch(ticket))
                    and self.store.authorize_launch(run_id)
                ):
                    failure_code = "execution_failed"
                    command = unit_command(unit, root, min(remaining, 3600), codex_arguments())
                    lease.verify()
                    code, events, ended = execute(
                        command, root.parent / "prompt.txt", self.store, run_id
                    )
                    lease.verify()
                    if not ended:
                        self.store.stop(run_id, "interrupted")
                        raise ApiError(
                            503, "termination_unknown", "进程结束尚未确认，占用继续保留。"
                        )
                    if (
                        code == 0
                        and events.completed == 1
                        and not events.failed
                        and not events.invalid
                    ):
                        failure_code = "invalid_result"
                        answer = root / "runtime/answer.json"
                        if answer.is_symlink() or answer.stat().st_size > 256 * 1024:
                            raise ValueError("Invalid result file")
                        result = ReplyResult.model_validate_json(answer.read_bytes())
                        if self._publish_result(run_id, result, code, events, ended):
                            return self.store.get(run_id)
                    self.store.finish_failed(run_id, failure_code)
        except KeyboardInterrupt:
            self.store.stop(run_id, "cancelled")
        except ApiError:
            raise
        except (ValueError, OSError, KeyError, subprocess.SubprocessError):
            self.store.stop(run_id, "interrupted")
        finally:
            self._remove_credential(run_id)
        if self.store.get(run_id)["status"] in {"queued", "running", "stopping"}:
            if not stop_unit(unit):
                raise ApiError(503, "termination_unknown", "尚未确认进程结束，占用继续保留。")
            self.store.finish_failed(run_id, failure_code)
        return self.store.get(run_id)


def interrupt_as_cancel(signum, frame):
    raise KeyboardInterrupt


def install_stop_handler():
    signal.signal(signal.SIGTERM, interrupt_as_cancel)

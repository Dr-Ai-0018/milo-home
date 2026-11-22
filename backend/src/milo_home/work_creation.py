"""Explicit native main creation and evidence-only recovery, without model turns."""

import os
import time
from pathlib import Path
from uuid import uuid4

from .codex_config import CodexRuntime, NewawzConfig, private_read
from .codex_rpc import ProtocolError
from .database_identity import private_run_directory
from .execution_control import execution_busy
from .execution_lock import execution_lock
from .scheduler_cli import StopRequest, stop_signals
from .work_access import require_access, require_session
from .work_attempt_history import attempt_matches, attempt_rows, read_history
from .work_creation_process import run_protocol
from .work_creation_records import (
    CHOICE,
    CreationRecords,
    choice_digest,
    encoded,
    ensure_not_abandoned,
    original_binding,
    process_states,
    read_choice,
    resume_verified,
)
from .work_creation_successor import SuccessorAuthorization
from .work_initialization import _context, register_main_locked
from .work_private_files import publish_file
from .work_receiver import ReceiveLimit
from .work_runtime import WorkRuntime
from .work_stop import stop_family


class MainCreation:
    def __init__(self, database, directory, owner, session_id, lease, limit, stop):
        self.database = database
        self.directory = directory
        self.owner = owner
        self.session_id = session_id
        self.lease = lease
        self.limit = limit
        self.stop = stop

    def create(self, installed, *, predecessor=None):
        context, self.access, associated = _context(
            self.database, self.directory, self.owner, self.session_id, self.lease
        )
        if associated or any(self.directory.iterdir()):
            raise ProtocolError(
                "Creation requires an empty dedicated home; recover the original choice"
            )
        successor = None
        if predecessor is None:
            with self.database.connect() as connection:
                if connection.execute(
                    "SELECT 1 FROM work_main_creations WHERE session_id=?", (self.session_id,)
                ).fetchone():
                    raise ProtocolError("Original main creation choice already exists")
        else:
            successor = SuccessorAuthorization.prepare(self, context, *predecessor)
        self.config = NewawzConfig.load()
        self.installed = installed
        version = installed.verify()
        self.credential = os.environ.get(self.config.env_key, "")
        installed.environment(self.directory, self.config, self.credential)
        self.limit.remaining()
        self.choice = {
            "schema": 1,
            "id": str(uuid4()),
            **context,
            "provider": self.config.provider,
            "model": self.config.model,
            "config_digest": self.config.digest,
            "version": version,
            "cli": str(installed.cli),
            "tool_host": str(installed.tool_host),
            "created_at": int(self.limit.last_wall),
            "deadline_at": int(self.limit.wall_deadline),
            "owner_revision": self.access[0],
            "workspace_revision": self.access[1],
        }
        if successor is not None:
            self.choice.update(successor.choice_fields())
        current, access, associated = _context(
            self.database, self.directory, self.owner, self.session_id, self.lease
        )
        if current != context or access != self.access or associated or self.stop.is_set():
            raise ProtocolError("Creation preparation identity or authorization changed")
        publish_file(self.directory / CHOICE, encoded(self.choice))
        self.records = CreationRecords(self.directory, create=True)
        publish_file(self.directory / "config.toml", self.config.content)
        if successor is not None:
            successor.publish(self.directory, self.choice)
        self._claim(successor)
        try:
            run_protocol(self)
            return self._finish()
        except BaseException:
            self._unknown()
            raise

    def _claim(self, successor):
        self.limit.remaining()
        if self.stop.is_set():
            raise ProtocolError("Creation stop requested before claiming")
        WorkRuntime(self.database, None)._identity(self.lease)
        with self.database.connect(write=True) as connection:
            session = require_session(connection, self.owner, self.session_id)
            access = require_access(connection, self.owner, self.choice["workspace_id"])
            root = connection.execute(
                "SELECT root_path FROM workspaces WHERE id=?", (session["workspace_id"],)
            ).fetchone()[0]
            if (
                session["workspace_id"] != self.choice["workspace_id"]
                or root != self.choice["cwd"]
                or tuple(access) != self.access
                or execution_busy(connection)
                or connection.execute(
                    "SELECT 1 FROM work_runtime_bindings WHERE session_id=?", (self.session_id,)
                ).fetchone()
            ):
                raise ProtocolError("Creation authorization or occupancy changed")
            self.limit.remaining()
            if self.stop.is_set():
                raise ProtocolError("Creation stop requested before claiming")
            if successor is not None:
                successor.claim(connection, self.session_id, self.choice)
            connection.execute(
                "INSERT INTO work_main_creations "
                "(id,session_id,choice_digest,owner_revision,workspace_revision,"
                "created_at,deadline_at,state,supersedes_id,authorization_id) "
                "VALUES (?,?,?,?,?,?,?, 'held',?,?)",
                (
                    self.choice["id"],
                    self.session_id,
                    choice_digest(self.choice),
                    *self.access,
                    self.choice["created_at"],
                    self.choice["deadline_at"],
                    self.choice.get("supersedes_id"),
                    self.choice.get("authorization_id"),
                ),
            )
        WorkRuntime(self.database, None)._identity(self.lease)

    def checkpoint(self):
        self.limit.remaining()
        if self.stop.is_set():
            raise ProtocolError("Main creation stop requested")
        WorkRuntime(self.database, None)._identity(self.lease)
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            self._check_transaction(connection)
        WorkRuntime(self.database, None)._identity(self.lease)
        root = Path(self.choice["cwd"])
        if root.resolve() != root or not root.is_dir():
            raise ProtocolError("Creation workspace is no longer canonical")
        if private_read(self.directory / "config.toml") != self.config.content:
            raise ProtocolError("Creation configuration changed")

    def _check_transaction(self, connection):
        session = require_session(connection, self.owner, self.session_id)
        access = require_access(connection, self.owner, session["workspace_id"])
        root = connection.execute(
            "SELECT root_path FROM workspaces WHERE id=?", (session["workspace_id"],)
        ).fetchone()[0]
        row = connection.execute(
            "SELECT * FROM work_main_creations WHERE id=?", (self.choice["id"],)
        ).fetchone()
        other = connection.execute(
            "SELECT 1 FROM runs WHERE status IN ('queued','running','stopping') "
            "UNION ALL SELECT 1 FROM execution_windows WHERE state!='closed' "
            "UNION ALL SELECT 1 FROM work_main_creations WHERE state!='closed' AND id!=? LIMIT 1",
            (self.choice["id"],),
        ).fetchone()
        if (
            tuple(access) != self.access
            or (
                self.choice.get("supersedes_id") is not None
                and self.access
                != (self.choice["owner_revision"], self.choice["workspace_revision"])
            )
            or session["workspace_id"] != self.choice["workspace_id"]
            or root != self.choice["cwd"]
            or row is None
            or not attempt_matches(row, self.choice)
            or row["session_id"] != self.session_id
            or connection.execute(
                "SELECT 1 FROM work_main_creations WHERE supersedes_id=?", (row["id"],)
            ).fetchone()
            or other is not None
        ):
            raise ProtocolError("Creation identity or authorization changed")

    def _unknown(self):
        WorkRuntime(self.database, None)._identity(self.lease)
        with self.database.connect(write=True) as connection:
            connection.execute(
                "UPDATE work_main_creations SET state='unknown' WHERE id=? AND state!='closed'",
                (self.choice["id"],),
            )

    def _finish(self):
        self.checkpoint()
        binding = original_binding(self.records, self.choice)
        if not resume_verified(self.records, self.choice, binding):
            run_protocol(self, binding=binding)
        if set(process_states(self.records).values()) != {"stop_confirmed"}:
            raise ProtocolError("Creation process still needs reconciliation")
        if not resume_verified(self.records, self.choice, binding):
            raise ProtocolError("Main persistence has not been verified")
        self.checkpoint()
        with self.database.connect(write=True) as connection:
            self._check_transaction(connection)
            connection.execute(
                "UPDATE work_main_creations SET state='closed' WHERE id=?", (self.choice["id"],)
            )
        result = register_main_locked(
            self.database,
            self.directory,
            self.owner,
            self.session_id,
            binding.thread_id,
            self.installed,
            self.lease,
            expected_access=self.access,
        )
        return result | {"protocol_verified": True, "persistence_verified": True}

    def load_original(self):
        self.choice = read_choice(self.directory)
        path, device, inode = WorkRuntime(self.database, None)._identity(self.lease)
        if any(
            self.choice.get(k) != v
            for k, v in {
                "database": path,
                "database_device": device,
                "database_inode": inode,
                "database_owner": self.lease.owner,
                "owner_id": self.owner,
                "session_id": self.session_id,
                "codex_home": str(self.directory),
            }.items()
        ):
            raise ProtocolError("Original creation identity changed")
        self.records = CreationRecords(self.directory)

    def recover(self):
        self.load_original()
        ensure_not_abandoned(self.records)
        self.verify_attempt_history()
        self.config = NewawzConfig.load()
        if (
            self.config.digest != self.choice["config_digest"]
            or self.config.provider != self.choice["provider"]
            or self.config.model != self.choice["model"]
        ):
            raise ProtocolError("Original creation configuration changed")
        self.installed = CodexRuntime(Path(self.choice["cli"]), Path(self.choice["tool_host"]))
        self.installed.verify()
        with self.database.connect() as connection:
            self.access = tuple(require_access(connection, self.owner, self.choice["workspace_id"]))
        self.checkpoint()
        try:
            self._stop_originals()
            # Neither creation nor injection is ever repeated by recovery.
            original_binding(self.records, self.choice)
            self.credential = os.environ.get(self.config.env_key, "")
            return self._finish()
        except BaseException:
            self._unknown()
            raise

    def verify_attempt_history(self):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            rows = attempt_rows(connection, self.session_id)
        read_history(self.directory, self.choice, *rows)

    def _stop_originals(self):
        launched = {
            e["connection"]
            for e in self.records.events
            if e.get("kind") == "process" and e.get("phase") == "launch_intent"
        }
        for unit in launched:
            ready = [
                e
                for e in self.records.events
                if e.get("connection") == unit
                and e.get("kind") == "process"
                and e.get("phase") == "ready"
            ]
            if len(ready) != 1:
                raise ProtocolError("Missing original creation process identity")
            identity = {
                k: ready[0][k] for k in ["unit", "group", "boot_id", "invocation", "main_pid"]
            }
            if not stop_family(identity):
                raise ProtocolError("Original creation stop needs reconciliation")
            self.records.record({"kind": "process", "phase": "stop_confirmed", "connection": unit})


def handle(database, directory, args):
    private_run_directory(directory)
    stop = StopRequest()
    with stop_signals(stop), execution_lock(database) as lease:
        limit = ReceiveLimit(args.timeout, time.time)
        claim = MainCreation(
            database, directory, str(args.owner), str(args.session), lease, limit, stop
        )
        if args.setup_action in {"create-main", "create-main-successor"}:
            predecessor = (
                (args.previous_home, str(args.supersede))
                if args.setup_action == "create-main-successor"
                else None
            )
            return claim.create(CodexRuntime(args.cli, args.tool_host), predecessor=predecessor)
        if args.setup_action == "abandon-main":
            from .work_creation_abandon import abandon

            return abandon(claim)
        return claim.recover()


def add_commands(commands):
    from uuid import UUID

    for name, help_ in (
        ("create-main", "显式创建专用主对话"),
        ("create-main-successor", "仅为已核销的原尝试授权一次新的初始化"),
        ("recover-main", "仅恢复原创建结果，不重建或补发"),
        ("abandon-main", "核对并放弃未完成的原初始化，仅释放执行占用"),
    ):
        command = commands.add_parser(name, help=help_)
        command.add_argument("--owner", type=UUID, required=True)
        command.add_argument("--session", type=UUID, required=True)
        command.add_argument("--timeout", type=int, required=True, help="1–3600秒，包含准备与核验")
        if name in {"create-main", "create-main-successor"}:
            command.add_argument("--cli", type=Path)
            command.add_argument("--tool-host", type=Path)
            if name == "create-main":
                command.add_argument("--create", action="store_true", required=True)
            else:
                command.add_argument("--previous-home", type=Path, required=True)
                command.add_argument("--supersede", type=UUID, required=True)
                command.add_argument("--authorize", action="store_true", required=True)
        else:
            command.add_argument("--confirm-stop", action="store_true", required=True)
        if name == "abandon-main":
            command.add_argument("--abandon", action="store_true", required=True)

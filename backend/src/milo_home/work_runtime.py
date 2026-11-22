"""Associate a business main session and claim one durable startup intention.

This module never spawns a process, sends an RPC or grants a turn permit.
The caller keeps the shared host lease through the future execution lifecycle.
"""

import os
import time
from dataclasses import dataclass
from uuid import uuid4

from .codex_execution_policy import NativeExecutionPolicy
from .codex_journal import BusinessBinding, PrivateJournal
from .codex_rpc import ProtocolError
from .errors import ApiError
from .execution_control import execution_busy
from .execution_lock import ExecutionIdentityError, ExecutionLease
from .execution_permits import active_window
from .native_policy import NativePolicyStore
from .work_access import require_access, require_session, require_task
from .work_creation_records import guard_registration
from .work_revisits import settle_window


@dataclass(frozen=True)
class StartupIntent:
    window_id: str
    task_id: str
    stream_id: str
    unit_name: str
    created_at: int
    deadline_at: int


def configured_native_runtime(
    database, directory, config, *, model=None, clock=time.time, read_only=False
):
    """Read one explicit native main; registration and business authorization remain separate."""
    journal = PrivateJournal(directory, initialize=False, read_only=read_only)
    runtime = WorkRuntime(database, journal, clock)
    if (
        runtime.native_policy is None
        or runtime.execution_policy is not NativeExecutionPolicy.AUTO_REVIEW
    ):
        raise ValueError("An explicitly registered native auto-review policy is required")
    if model is not None and model != runtime.native_policy.model:
        raise ValueError("Requested reply model differs from the registered native policy")
    runtime.verify_execution_rules()
    installed = journal.installation()
    installed.verify()
    journal.bind(journal.binding(), config, installed)
    return runtime


class WorkRuntime:
    def __init__(self, database, journal: PrivateJournal, clock=time.time):
        self.database = database
        self.journal = journal
        self.clock = clock
        self.native_policy_store = NativePolicyStore(journal) if journal is not None else None
        self.native_policy = self.native_policy_store.read() if self.native_policy_store else None

    def conversation_binding(self):
        if self.native_policy is None:
            return self.journal.binding()
        return self.native_policy.conversation_binding(self.journal)

    def verify_execution_rules(self):
        if self.native_policy is not None:
            self.native_policy_store.verify_instructions(self.native_policy)

    @property
    def execution_policy(self):
        if self.native_policy is None:
            return NativeExecutionPolicy.LEGACY_DIRECT
        return self.native_policy.execution

    def _identity(self, lease: ExecutionLease):
        lease.verify()
        if self.native_policy_store and self.native_policy_store.read() != self.native_policy:
            raise ProtocolError("Native policy changed during this execution context")
        if self.database.path.resolve(strict=True) != lease.canonical:
            raise ExecutionIdentityError("Host lease belongs to another database")
        if getattr(self.database, "uid", os.geteuid()) != lease.owner:
            raise ExecutionIdentityError("Host lease belongs to another database account")
        metadata = os.fstat(lease.descriptor)
        return str(lease.canonical), metadata.st_dev, metadata.st_ino

    def _snapshot(self, lease):
        path, device, inode = self._identity(lease)
        root = self.journal.binding().cwd
        if root.resolve() != root or not root.is_dir():
            raise ProtocolError("Main thread workspace is no longer canonical")
        return (
            {
                "database": path,
                "database_device": device,
                "database_inode": inode,
                "fingerprint": self.journal.fingerprint(),
            },
            str(root),
            self.journal.business_binding(),
        )

    @staticmethod
    def _binding(connection, owner, session_id, snapshot, root):
        session = require_session(connection, owner, session_id)
        workspace = connection.execute(
            "SELECT root_path FROM workspaces WHERE id=?", (session["workspace_id"],)
        ).fetchone()
        if workspace[0] != root:
            raise ProtocolError("Main thread does not match the registered workspace")
        return dict(
            session_id=session_id,
            owner_id=owner,
            workspace_id=session["workspace_id"],
            **snapshot,
        )

    def bind(
        self, owner: str, session_id: str, *, lease: ExecutionLease, expected_access=None
    ) -> BusinessBinding:
        snapshot, root, linked = self._snapshot(lease)
        successor_access = guard_registration(
            self.database,
            self.journal.path.parent,
            self.journal.binding(),
            owner,
            session_id,
            lease,
            self.journal.installation(),
        )
        if successor_access is not None:
            if expected_access is not None and successor_access != expected_access:
                raise ProtocolError("Successor authorization revision changed")
            expected_access = successor_access
        with self.database.connect(write=True) as connection:
            identity = self._binding(connection, owner, session_id, snapshot, root)
            if expected_access is not None:
                access = require_access(connection, owner, identity["workspace_id"])
                if tuple(access) != expected_access or execution_busy(connection):
                    raise ProtocolError("Registration authorization or occupancy changed")
            stored = connection.execute(
                "SELECT * FROM work_runtime_bindings WHERE session_id=?", (session_id,)
            ).fetchone()
            binding_id = stored["binding_id"] if stored else str(uuid4())
            binding = BusinessBinding(binding_id=binding_id, **identity)
            if linked is not None and linked != binding:
                raise ProtocolError("Private runtime belongs to another business binding")
            if stored and stored["fingerprint"] != binding.fingerprint:
                raise ProtocolError("Business main changed; reconciliation required")
            if stored is None:
                connection.execute(
                    "INSERT INTO work_runtime_bindings VALUES (?,?,?,?)",
                    (session_id, binding_id, binding.fingerprint, int(self.clock())),
                )
        # A crash here leaves an immutable business fingerprint; bind may only
        # finish the identical association, never select a replacement main.
        lease.verify()
        self.journal.bind_business(binding)
        lease.verify()
        return binding

    def claim_start(self, owner: str, window_id: str, *, lease: ExecutionLease) -> StartupIntent:
        snapshot, root, linked = self._snapshot(lease)
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            window = active_window(connection, owner, window_id, now)
            if window["state"] != "held":
                raise ApiError(409, "startup_state_changed", "启动状态已变化，请先核对。")
            task = connection.execute(
                "SELECT * FROM work_tasks WHERE id=?", (window["task_id"],)
            ).fetchone()
            if task["status"] != "queued":
                raise ApiError(409, "startup_state_changed", "启动状态已变化，请先核对。")
            identity = self._binding(connection, owner, task["session_id"], snapshot, root)
            stored = connection.execute(
                "SELECT * FROM work_runtime_bindings WHERE session_id=?", (task["session_id"],)
            ).fetchone()
            if stored is None or stored["fingerprint"] != identity["fingerprint"]:
                raise ProtocolError("Register the exact business main before startup")
            binding = BusinessBinding(binding_id=stored["binding_id"], **identity)
            if linked != binding:
                raise ProtocolError("Private business association needs reconciliation")
            if connection.execute(
                "SELECT 1 FROM execution_start_intents WHERE window_id=?", (window_id,)
            ).fetchone():
                raise ApiError(409, "startup_already_recorded", "已有启动记录，不能再次启动。")
            intent = StartupIntent(
                window_id,
                task["id"],
                uuid4().hex,
                window["unit_name"],
                window["created_at"],
                window["deadline_at"],
            )
            connection.execute(
                "INSERT INTO execution_start_intents VALUES (?,?,?,?)",
                (window_id, binding.binding_id, intent.stream_id, now),
            )
        # Commit the global claim first. A crash or private write failure cannot
        # make another coordinator win the same window and spawn it again.
        try:
            lease.verify()
            self.journal.open_work_stream(
                binding,
                unit=intent.unit_name,
                stream=intent.stream_id,
                window_id=window_id,
                task_id=intent.task_id,
            )
            lease.verify()
        except Exception:
            self._mark_unknown(intent, lease, now)
            raise
        return intent

    def check_task_binding(self, owner, task_id, *, lease):
        """Preflight an existing association; never finish or create a binding."""
        return self._check_existing_binding(owner, lease, task_id=task_id)

    def check_session_binding(self, owner, session_id, *, lease):
        return self._check_existing_binding(owner, lease, session_id=session_id)

    def _check_existing_binding(self, owner, lease, *, task_id=None, session_id=None):
        snapshot, root, linked = self._snapshot(lease)
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            if task_id is not None:
                session_id = require_task(connection, owner, task_id)["session_id"]
            identity = self._binding(connection, owner, session_id, snapshot, root)
            access = require_access(connection, owner, identity["workspace_id"])
            stored = connection.execute(
                "SELECT * FROM work_runtime_bindings WHERE session_id=?", (session_id,)
            ).fetchone()
            if (
                stored is None
                or stored["fingerprint"] != identity["fingerprint"]
                or linked != BusinessBinding(binding_id=stored["binding_id"], **identity)
            ):
                raise ProtocolError("Existing business main association is required")
        self._identity(lease)
        return linked, access["owner_revision"], access["workspace_revision"]

    def _mark_unknown(self, intent, lease, now):
        self._identity(lease)
        with self.database.connect(write=True) as connection:
            connection.execute(
                "UPDATE execution_windows SET state='unknown' WHERE id=? AND state!='closed'",
                (intent.window_id,),
            )
            connection.execute(
                "UPDATE work_tasks SET status='needs_reconciliation',updated_at=? WHERE id=?",
                (now, intent.task_id),
            )
            settle_window(connection, intent.window_id, now, unknown=True)
        lease.verify()

    def checkpoint(self, owner: str, intent: StartupIntent, *, lease: ExecutionLease):
        """Recheck the exact live intent without opening private files as service UID."""
        snapshot, root, linked = self._snapshot(lease)
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            window = active_window(connection, owner, intent.window_id, int(self.clock()))
            row = connection.execute(
                "SELECT * FROM execution_start_intents WHERE window_id=?", (intent.window_id,)
            ).fetchone()
            task = connection.execute(
                "SELECT session_id FROM work_tasks WHERE id=?", (window["task_id"],)
            ).fetchone()
            identity = self._binding(connection, owner, task[0], snapshot, root)
            if (
                row is None
                or linked is None
                or linked != BusinessBinding(binding_id=row["binding_id"], **identity)
                or row["stream_id"] != intent.stream_id
                or window["task_id"] != intent.task_id
                or window["unit_name"] != intent.unit_name
                or window["created_at"] != intent.created_at
                or window["deadline_at"] != intent.deadline_at
            ):
                raise ProtocolError("Live startup association changed")
        lease.verify()

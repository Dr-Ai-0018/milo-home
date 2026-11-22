"""Durable routing of fresh website inputs to one existing native conversation.

The caller holds the shared host lease. This ledger contains identities and
digests, never a second history or an application-managed summary.
"""

import os
import sqlite3
import stat
from contextlib import contextmanager
from uuid import UUID

from . import native_message_capacity
from .codex_config import private_create, private_directory, private_read
from .codex_rpc import ProtocolError
from .native_message_scope import NativeMessageEvent, NativeMessageScope
from .native_policy import NativePolicyStore


class NativeMessageStore:
    def __init__(self, journal):
        self.journal = journal
        self.directory = journal.path.parent
        self.path = self.directory / "native-message-links.sqlite3"
        self.required = self.directory / "native-message-links.required"

    def _identity(self, scope):
        linked = self.journal.business_binding()
        if (
            linked is None
            or str(scope.business_binding_id) != linked.binding_id
            or str(scope.owner_id) != linked.owner_id
            or scope.baseline_fingerprint != self.journal.fingerprint()
            or scope.native_thread_id != self.journal.binding().thread_id
        ):
            raise ProtocolError("Native message scope differs from the registered main")

    def claim(self, scope, *, lease):
        """Persist every input identity before launch; a used message cannot move runs."""
        self._lease(lease)
        self._identity(scope)
        policy_store = NativePolicyStore(self.journal)
        policy = policy_store.read()
        if policy is None or policy.digest != scope.policy_digest:
            raise ProtocolError("Native message policy is not the current registered policy")
        policy_store.verify_instructions(policy)
        with self._connect(write=True, create=True) as connection:
            saved = self._read(connection, str(scope.run_id))
            if saved is not None:
                if saved[0] != scope:
                    raise ProtocolError("Native message delivery changed")
                if any(event.kind == "sent" for event in saved[1]):
                    raise ProtocolError("Native input was already attempted; never resend")
                native_message_capacity.require_capacity(connection)
                return
            native_message_capacity.require_capacity(connection, additional=1)
            connection.execute(
                "INSERT INTO deliveries VALUES (?,?,?)",
                (str(scope.run_id), scope.encoded(), scope.digest),
            )
            try:
                connection.executemany(
                    "INSERT INTO messages VALUES (?,?)",
                    [(str(identity), str(scope.run_id)) for identity in scope.message_ids],
                )
                if scope.revisit is not None:
                    connection.execute(
                        "INSERT INTO revisits VALUES (?,?,?)",
                        (str(scope.revisit.id), scope.revisit.revision, str(scope.run_id)),
                    )
            except sqlite3.IntegrityError:
                raise ProtocolError("Native input belongs to an earlier delivery") from None
            native_message_capacity.require_capacity(connection)
        self._lease(lease)

    def capacity(self, *, additional=1):
        if not self.path.exists() and not self.path.is_symlink():
            if self.required.exists() or self.required.is_symlink():
                raise ProtocolError("Native message ledger is missing")
            return {"has_capacity": True, "unsettled_deliveries": 0}
        with self._connect() as connection:
            return native_message_capacity.capacity(connection, additional=additional)

    def require_capacity(self, *, additional=0):
        if not self.capacity(additional=additional)["has_capacity"]:
            raise ProtocolError("Native message retention capacity; cleanup space reserved")

    def _lease(self, lease):
        lease.verify()
        linked = self.journal.business_binding()
        metadata = os.fstat(lease.descriptor)
        if linked is None or (str(lease.canonical), metadata.st_dev, metadata.st_ino) != (
            linked.database,
            linked.database_device,
            linked.database_inode,
        ):
            raise ProtocolError("Native message lease belongs to another database")

    def inspect(self, run_id):
        if not self.path.exists() and not self.path.is_symlink():
            if self.required.exists() or self.required.is_symlink():
                raise ProtocolError("Native message ledger is missing")
            return None
        with self._connect() as connection:
            return self._read(connection, str(UUID(str(run_id))))

    def _read(self, connection, run_id):
        row = connection.execute(
            "SELECT body,digest FROM deliveries WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        scope = NativeMessageScope.model_validate_json(row[0])
        if scope.encoded() != row[0] or scope.digest != row[1] or str(scope.run_id) != run_id:
            raise ProtocolError("Native message evidence differs")
        self._identity(scope)
        ids = {
            UUID(row[0])
            for row in connection.execute(
                "SELECT message_id FROM messages WHERE run_id=?", (run_id,)
            )
        }
        if ids != set(scope.message_ids):
            raise ProtocolError("Native message routing evidence differs")
        if scope.revisit is not None:
            if connection.execute("PRAGMA user_version").fetchone()[0] < 2:
                raise ProtocolError("Native revisit routing evidence is missing")
            rows = [
                tuple(row)
                for row in connection.execute(
                    "SELECT revisit_id,revision FROM revisits WHERE run_id=?", (run_id,)
                )
            ]
            if rows != [(str(scope.revisit.id), scope.revisit.revision)]:
                raise ProtocolError("Native revisit routing evidence differs")
        events = []
        for row in connection.execute(
            "SELECT kind,turn_id,body FROM events WHERE run_id=? ORDER BY sequence", (run_id,)
        ):
            event = NativeMessageEvent.model_validate_json(row[2])
            if event.kind != row[0] or event.turn_id != row[1] or event.encoded() != row[2]:
                raise ProtocolError("Native message event evidence differs")
            self._transition(events, event)
            if len(event.encoded().encode()) > 4096:
                raise ProtocolError("Native message cleanup evidence size limit")
            events.append(event)
        return scope, events

    def record(self, scope, event, *, lease):
        self._lease(lease)
        self._identity(scope)
        with self._connect(write=True) as connection:
            saved = self._read(connection, str(scope.run_id))
            if saved is None or saved[0] != scope:
                raise ProtocolError("Native message delivery must be claimed first")
            events = saved[1]
            prior = next((item for item in reversed(events) if item.kind == event.kind), None)
            if prior is not None:
                if prior == event and event.kind != "sent":
                    return
                if event.kind != "stopped" or prior.stop_confirmed:
                    raise ProtocolError("Native message event cannot be repeated or replaced")
            self._transition(events, event)
            try:
                if len(event.encoded().encode()) > 4096:
                    raise ProtocolError("Native message cleanup evidence size limit")
                connection.execute(
                    "INSERT INTO events(run_id,kind,turn_id,body) VALUES (?,?,?,?)",
                    (str(scope.run_id), event.kind, event.turn_id, event.encoded()),
                )
            except sqlite3.IntegrityError:
                raise ProtocolError("Native turn belongs to another message delivery") from None
        self._lease(lease)

    @staticmethod
    def _transition(events, event):
        if event.kind == "sent":
            valid = not events and event.turn_id is None and event.outcome is None
        else:
            first = events[0] if events else None
            valid = (
                first is not None
                and first.kind == "sent"
                and all(
                    (item.stream_id, item.request_id) == (event.stream_id, event.request_id)
                    for item in events
                )
                and not any(item.kind == "stopped" and item.stop_confirmed for item in events)
            )
            accepted = next((item for item in events if item.kind == "accepted"), None)
            if event.kind == "accepted":
                valid = valid and len(events) == 1 and event.turn_id is not None
            elif event.kind == "terminal":
                valid = valid and accepted is not None and event.turn_id == accepted.turn_id
                valid = valid and event.outcome is not None
            else:
                valid = valid and event.stop_confirmed is not None
                valid = valid and event.turn_id == (accepted.turn_id if accepted else None)
            if event.kind != "stopped" and any(item.kind == "stopped" for item in events):
                valid = False
        if event.kind != "terminal" and (
            event.outcome is not None or event.result_digest is not None
        ):
            valid = False
        if event.kind != "stopped" and event.stop_confirmed is not None:
            valid = False
        if event.result_digest is not None and event.outcome != "completed":
            valid = False
        if not valid:
            raise ProtocolError("Native message event order or turn differs")

    @contextmanager
    def _connect(self, *, write=False, create=False):
        private_directory(self.directory)
        exists = self.path.exists() or self.path.is_symlink()
        if not exists and (self.required.exists() or self.required.is_symlink()):
            raise ProtocolError("Native message ledger is missing")
        if create and not exists:
            private_create(self.required, b"1\n")
            private_create(self.path, b"")
        if private_read(self.required) != b"1\n":
            raise ProtocolError("Native message ledger marker differs")
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = self.path.with_name(self.path.name + suffix)
            if suffix and not path.exists() and not path.is_symlink():
                continue
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
            ):
                raise ProtocolError("Native message ledger must be private and non-linked")
        connection = sqlite3.connect(
            self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
            uri=True,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            if write:
                connection.execute(f"PRAGMA max_page_count={native_message_capacity.PAGE_LIMIT}")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and create and not exists:
                connection.execute(
                    "CREATE TABLE deliveries("
                    "run_id TEXT PRIMARY KEY,"
                    "body TEXT NOT NULL,"
                    "digest TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE messages("
                    "message_id TEXT PRIMARY KEY,"
                    "run_id TEXT NOT NULL REFERENCES deliveries(run_id))"
                )
                connection.execute(
                    "CREATE TABLE events("
                    "sequence INTEGER PRIMARY KEY,"
                    "run_id TEXT NOT NULL REFERENCES deliveries(run_id),"
                    "kind TEXT NOT NULL,"
                    "turn_id TEXT,"
                    "body TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX event_once ON events(run_id,kind) WHERE kind!='stopped'"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX native_turn_once ON events(turn_id) WHERE kind='accepted'"
                )
                for table in ("deliveries", "messages", "events"):
                    for operation in ("UPDATE", "DELETE"):
                        connection.execute(
                            f"CREATE TRIGGER immutable_{table}_{operation.lower()} "
                            f"BEFORE {operation} ON {table} "
                            "BEGIN SELECT RAISE(ABORT,'Native delivery is immutable'); END"
                        )
                connection.execute("PRAGMA user_version=1")
                version = 1
            if version == 1 and write:
                connection.execute(
                    "CREATE TABLE revisits (revisit_id TEXT NOT NULL, "
                    "revision INTEGER NOT NULL, run_id TEXT NOT NULL UNIQUE "
                    "REFERENCES deliveries(run_id), PRIMARY KEY (revisit_id,revision))"
                )
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER immutable_revisits_{operation.lower()} "
                        f"BEFORE {operation} ON revisits "
                        "BEGIN SELECT RAISE(ABORT,'Native revisit is immutable'); END"
                    )
                connection.execute("PRAGMA user_version=2")
            elif version not in {1, 2}:
                raise ProtocolError("Native message ledger registration is incomplete")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

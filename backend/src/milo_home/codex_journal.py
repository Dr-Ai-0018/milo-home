"""Private runtime evidence, separate from the business database and public events."""

import hashlib
import json
import re
import sqlite3
import stat
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from . import journal_capacity, journal_segments, journal_storage, journal_streams
from .codex_app_server import ThreadBinding
from .codex_config import (
    CodexRuntime,
    NewawzConfig,
    private_create,
    private_directory,
    private_read,
    same_installation_identity,
)
from .codex_process import native_unit_name
from .codex_rpc import METHODS, ProtocolError
from .work_receipts import receipt_shape
from .work_stop import stop_shape

SCHEMA = (
    "CREATE TABLE binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)",
    "CREATE TABLE streams (id TEXT PRIMARY KEY, unit TEXT NOT NULL UNIQUE, "
    "state TEXT NOT NULL CHECK(state IN ('open','unknown','closed')), "
    "bytes INTEGER NOT NULL DEFAULT 0)",
    "CREATE UNIQUE INDEX one_writer ON streams((1)) WHERE state != 'closed'",
    "CREATE TABLE requests (stream TEXT NOT NULL REFERENCES streams(id), "
    "request_id INTEGER NOT NULL, method TEXT NOT NULL, "
    "state TEXT NOT NULL CHECK(state IN ('pending','acknowledged','unknown')), "
    "PRIMARY KEY(stream,request_id))",
    "CREATE TABLE entries (sequence INTEGER PRIMARY KEY, "
    "stream TEXT NOT NULL REFERENCES streams(id), "
    "payload TEXT NOT NULL)",
)

BUSINESS_SCHEMA = (
    "CREATE TABLE business_binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "value TEXT NOT NULL)",
    "CREATE TABLE stream_work (stream TEXT PRIMARY KEY REFERENCES streams(id), "
    "window_id TEXT NOT NULL UNIQUE, task_id TEXT NOT NULL)",
)


@dataclass(frozen=True)
class BusinessBinding:
    binding_id: str
    session_id: str
    owner_id: str
    workspace_id: str
    database: str
    database_device: int
    database_inode: int
    fingerprint: str


class PrivateJournal:
    def __init__(self, directory: Path, *, initialize=True, read_only=False):
        private_directory(directory)
        self.path = directory / "session-journal.sqlite3"
        self.read_only = read_only
        if not initialize:
            with self._connect() as connection:
                if connection.execute("PRAGMA user_version").fetchone()[0] not in {2, 3}:
                    raise ProtocolError("Existing runtime journal version is required")
            return
        if read_only:
            raise ValueError("Read-only journals cannot be initialized")
        try:
            private_create(self.path, b"")
        except FileExistsError:
            pass
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                    raise ProtocolError("Unversioned runtime journal is not empty")
                for statement in SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version=1")
                version = 1
            if version == 1:
                for statement in BUSINESS_SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version=2")
            elif version not in {2, 3}:
                raise ProtocolError("Unsupported runtime journal version")

    @contextmanager
    def _connect(self):
        private_directory(self.path.parent)
        # The directory is caller-owned; no untrusted writer may replace SQLite files.
        for suffix in ["", "-journal", "-wal", "-shm"]:
            path = Path(str(self.path) + suffix)
            if suffix and not path.exists() and not path.is_symlink():
                continue
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self.path.parent.stat().st_uid
                or metadata.st_mode & 0o077
            ):
                raise ProtocolError("Runtime journal files must be private and non-linked")
        mode = "ro" if self.read_only else "rw"
        connection = sqlite3.connect(
            self.path.as_uri() + "?mode=" + mode, uri=True, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN" if self.read_only else "BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def bind(self, binding: ThreadBinding, config: NewawzConfig, runtime: CodexRuntime):
        if (
            binding.cwd.resolve() != binding.cwd
            or not binding.cwd.is_dir()
            or binding.provider != config.provider
            or binding.model != config.model
            or not runtime.cli.is_absolute()
            or not runtime.tool_host.is_absolute()
            or private_read(self.path.parent / "config.toml") != config.content
        ):
            raise ProtocolError("Main binding differs from registered configuration")
        value = {
            "thread_id": binding.thread_id,
            "provider": binding.provider,
            "model": binding.model,
            "cwd": str(binding.cwd),
            "codex_home": str(self.path.parent),
            "config_digest": config.digest,
            "cli": str(runtime.cli),
            "tool_host": str(runtime.tool_host),
        }
        with self._connect() as connection:
            stored = connection.execute("SELECT value FROM binding WHERE singleton=1").fetchone()
            if stored is None and not self.read_only:
                encoded = json.dumps(value, sort_keys=True)
                connection.execute("INSERT INTO binding VALUES (1, ?)", (encoded,))
            elif stored is None or not same_installation_identity(json.loads(stored[0]), value):
                raise ProtocolError("Persistent main binding changed; reconciliation required")

    def binding(self) -> ThreadBinding:
        value = self.binding_record()
        if value is None:
            raise ProtocolError("No persistent main binding")
        return ThreadBinding(
            value["thread_id"], value["provider"], value["model"], Path(value["cwd"])
        )

    def binding_record(self):
        with self._connect() as connection:
            stored = connection.execute("SELECT value FROM binding WHERE singleton=1").fetchone()
            return json.loads(stored[0]) if stored else None

    def installation(self) -> CodexRuntime:
        value = self.binding_record()
        if (
            not isinstance(value, dict)
            or value.get("codex_home") != str(self.path.parent)
            or any(not isinstance(value.get(key), str) for key in ("cli", "tool_host"))
        ):
            raise ProtocolError("Existing runtime installation differs from its binding")
        return CodexRuntime(Path(value["cli"]), Path(value["tool_host"]))

    def open_stream(self, unit: str) -> str:
        stream = uuid4().hex
        with self._connect() as connection:
            self._open_stream(connection, unit, stream)
        return stream

    @staticmethod
    def _open_stream(connection, unit: str, stream: str):
        if not native_unit_name(unit):
            raise ValueError("Owned execution family identity is required")
        if not re.fullmatch(r"[0-9a-f]{32}", stream):
            raise ValueError("Exact durable stream identity is required")
        if connection.execute("SELECT 1 FROM binding").fetchone() is None:
            raise ProtocolError("Bind the main thread before opening a stream")
        if connection.execute("SELECT 1 FROM streams WHERE state != 'closed'").fetchone():
            raise ProtocolError("Previous execution evidence needs reconciliation")
        journal_capacity.require_admission(connection)
        connection.execute(
            "INSERT INTO streams (id,unit,state) VALUES (?,?,'open')", (stream, unit)
        )

    def fingerprint(self) -> str:
        with self._connect() as connection:
            return self._fingerprint(connection)

    @staticmethod
    def _fingerprint(connection) -> str:
        row = connection.execute("SELECT value FROM binding WHERE singleton=1").fetchone()
        if row is None:
            raise ProtocolError("No persistent main binding")
        return hashlib.sha256(row[0].encode()).hexdigest()

    def business_binding(self) -> BusinessBinding | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM business_binding WHERE singleton=1"
            ).fetchone()
            return BusinessBinding(**json.loads(row[0])) if row else None

    def bind_business(self, binding: BusinessBinding):
        value = json.dumps(asdict(binding), sort_keys=True)
        with self._connect() as connection:
            if self._fingerprint(connection) != binding.fingerprint:
                raise ProtocolError("Business binding differs from private main")
            row = connection.execute(
                "SELECT value FROM business_binding WHERE singleton=1"
            ).fetchone()
            if row and row[0] != value:
                raise ProtocolError("Business binding changed; reconciliation required")
            if row is None:
                connection.execute("INSERT INTO business_binding VALUES (1,?)", (value,))

    def open_work_stream(
        self, binding: BusinessBinding, *, unit: str, stream: str, window_id: str, task_id: str
    ):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM business_binding WHERE singleton=1"
            ).fetchone()
            if row is None or json.loads(row[0]) != asdict(binding):
                raise ProtocolError("Business binding does not match this execution")
            self._open_stream(connection, unit, stream)
            connection.execute(
                "INSERT INTO stream_work VALUES (?,?,?)", (stream, window_id, task_id)
            )

    def recorder(self, stream: str) -> Callable:
        return lambda event: self.record(stream, event)

    def capacity(self):
        with self._connect() as connection:
            return journal_capacity.capacity(connection)

    def require_capacity(self):
        with self._connect() as connection:
            journal_capacity.require_admission(connection)

    def require_turn_capacity(self, stream):
        with self._connect() as connection:
            journal_capacity.require_turn_capacity(connection, stream)

    def record(self, stream: str, event: dict):
        payload = journal_capacity.encode(event)
        size = len(payload.encode())
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM streams WHERE id=?", (stream,)).fetchone()
            if row is None or (row["state"] != "open" and event.get("kind") != "work_stop"):
                raise ProtocolError("Private journal stream unavailable")
            journal_capacity.check_append(
                connection, row, size, reserve=journal_capacity.event_reserve(event, size)
            )
            kind = event.get("kind")
            if kind == "intent":
                self._intent(connection, stream, event)
            elif kind in {"response", "unknown"}:
                request_id = event.get("id")
                if type(request_id) is not int:
                    raise ProtocolError("Missing request identity")
                changed = connection.execute(
                    "UPDATE requests SET state=? WHERE stream=? AND request_id=? "
                    + ("AND state='pending'" if kind == "response" else ""),
                    ("acknowledged" if kind == "response" else "unknown", stream, request_id),
                ).rowcount
                if changed != 1:
                    raise ProtocolError("Unexpected durable request result")
                if kind == "unknown":
                    connection.execute("UPDATE streams SET state='unknown' WHERE id=?", (stream,))
            elif kind != "notification":
                if kind == "work_terminal":
                    receipt_shape(event)
                elif kind == "work_stop":
                    stop_shape(event)
                    if event["unit"] != row["unit"]:
                        raise ProtocolError("Recovery evidence differs from its stream")
                elif kind == "process":
                    journal_streams.validate_process(row, event)
                else:
                    raise ProtocolError("Unsupported private journal record")
            sequence = journal_storage.insert_entry(connection, stream, payload)
            connection.execute("UPDATE streams SET bytes=bytes+? WHERE id=?", (size, stream))
            return sequence

    def reconcile_stream(self, stream, stop_sequence):
        """A fresh appended stop proof cannot repair an unacknowledged RPC."""
        with self._connect() as connection:
            entries = journal_streams.snapshot(connection, stream)["entries"]
            if not entries or entries[-1][0] != stop_sequence:
                raise ProtocolError("Fresh stream stop evidence is required")
            stop_shape(entries[-1][1])
            if connection.execute(
                "SELECT 1 FROM requests WHERE stream=? AND state!='acknowledged'", (stream,)
            ).fetchone():
                raise ProtocolError("Unknown delivery still needs reconciliation")
            connection.execute("UPDATE streams SET state='closed' WHERE id=?", (stream,))

    def work_snapshot(self, stream):
        """Root-private bounded evidence; never expose this snapshot through HTTP."""
        result = self.stream_snapshot(stream)
        if result["work"] is None:
            raise ProtocolError("Private work stream is missing")
        return result

    def stream_snapshot(self, stream):
        with self._connect() as connection:
            return journal_streams.snapshot(connection, stream)

    def storage_summary(self, *, budget=journal_segments.STORAGE_LIMIT):
        with self._connect() as connection:
            return journal_segments.summary(connection, budget=budget)

    def segment_catalogue(self):
        with self._connect() as connection:
            return journal_segments.catalogue(connection)

    def enable_segmentation(self):
        with self._connect() as connection:
            journal_storage.migrate(connection)

    def list_streams(self, *, unit=None, limit=None):
        with self._connect() as connection:
            return journal_streams.list_streams(connection, unit=unit, limit=limit)

    def process_events(self, stream, *, limit):
        with self._connect() as connection:
            return journal_streams.process_events(connection, stream, limit=limit)

    def append_repair(self, expected, receipts, marker, proof):
        with self._connect() as connection:
            return journal_streams.append_repair(connection, expected, receipts, marker, proof)

    @staticmethod
    def _intent(connection, stream, event):
        request = event.get("request", {})
        request_id = request.get("id")
        if type(request_id) is not int or request_id < 1 or request.get("method") not in METHODS:
            raise ProtocolError("Invalid durable request intent")
        pending = connection.execute(
            "SELECT 1 FROM requests WHERE stream=? AND state!='acknowledged'", (stream,)
        ).fetchone()
        if pending:
            raise ProtocolError("Earlier delivery requires reconciliation")
        connection.execute(
            "INSERT INTO requests VALUES (?,?,?,'pending')", (stream, request_id, request["method"])
        )

    def outstanding(self) -> list[dict]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT streams.id,unit,streams.state,request_id,method,"
                    "requests.state AS delivery "
                    "FROM streams LEFT JOIN requests ON streams.id=requests.stream "
                    "AND requests.state!='acknowledged' WHERE streams.state!='closed' "
                    "ORDER BY streams.id,request_id"
                )
            ]

    def close_stream(self, stream: str, *, stop: Callable) -> bool:
        # Confirmation belongs to the execution-family owner, never to RPC acknowledgements.
        if not stop():
            return False
        with self._connect() as connection:
            row = connection.execute("SELECT state FROM streams WHERE id=?", (stream,)).fetchone()
            if row is None:
                raise ProtocolError("Unknown private journal stream")
            unresolved = connection.execute(
                "SELECT 1 FROM requests WHERE stream=? AND state!='acknowledged'", (stream,)
            ).fetchone()
            state = "unknown" if unresolved or row[0] == "unknown" else "closed"
            connection.execute("UPDATE streams SET state=? WHERE id=?", (state, stream))
            return state == "closed"

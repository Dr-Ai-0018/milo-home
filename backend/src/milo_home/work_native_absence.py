"""Verify an empty, stopped native home without opening its original SQLite files."""

import hashlib
import os
import re
import sqlite3
import stat
import tempfile
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from .codex_rpc import ProtocolError
from .work_creation_records import CHOICE, EVENTS, HISTORY, encoded

_FILE_LIMIT = 8 * 1024 * 1024
_AUXILIARY = {
    "goals": {"thread_goals"},
    "logs": {"logs"},
    "memories": {"stage1_outputs", "jobs"},
    "queue": {"queued_items", "queued_thread_revisions"},
}
_BOOKKEEPING = {
    "_sqlx_migrations",
    "sqlite_sequence",
    "backfill_state",
    "thread_sections",
    "consolidation_progress",
}
_NATIVE_FILE = re.compile(r"(state|goals|logs|memories|queue)_\d+\.sqlite(?:-(wal|shm))?")


def _metadata(path, *, directory=False):
    value = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
        raise ProtocolError("Unexpected native home file identity")
    return value


def _read_native(path, *, limit=_FILE_LIMIT):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        before = _metadata(path)
        opened = os.fstat(stream.fileno())
        if opened != before or opened.st_size > limit:
            raise ProtocolError("Native evidence exceeds the file or identity bound")
        data = stream.read(limit + 1)
        after = path.stat(follow_symlinks=False)
        if len(data) != opened.st_size or (
            after.st_ino,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_size,
        ) != (opened.st_ino, opened.st_mtime_ns, opened.st_ctime_ns, opened.st_size):
            raise ProtocolError("Native evidence changed during read")
        return data


def _check_database(path, required, *, immutable):
    uri = path.as_uri() + ("?mode=ro&immutable=1" if immutable else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        tables = connection.execute("PRAGMA table_list").fetchall()
        ordinary = {
            name for schema, name, kind, *_ in tables if schema == "main" and kind == "table"
        }
        if not required <= ordinary or any(
            kind in {"virtual", "shadow"} for _, _, kind, *_ in tables
        ):
            raise ProtocolError("Native evidence does not have the required ordinary tables")
        if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
            raise ProtocolError("Native database integrity could not be verified")
        for table in sorted(ordinary - _BOOKKEEPING - {"sqlite_schema"}):
            identifier = '"' + table.replace('"', '""') + '"'
            if connection.execute(f"SELECT 1 FROM {identifier} LIMIT 1").fetchone():
                raise ProtocolError("Native home contains persisted work or conversation data")


def verify_native_absence(home):
    files = defaultdict(dict)
    digests = {}
    for path in sorted(home.iterdir()):
        name = path.name
        if name in {CHOICE, EVENTS, HISTORY, "config.toml"}:
            continue
        if name in {".sandbox_migration", "installation_id"}:
            digests[name] = hashlib.sha256(_read_native(path, limit=1024)).hexdigest()
            continue
        if name in {"skills", "tmp", "thread-writer-locks"}:
            _metadata(path, directory=True)
            if name == "tmp":
                for child in path.iterdir():
                    _metadata(child, directory=True)
                    if child.name != "arg0-linux" or next(child.iterdir(), None) is not None:
                        raise ProtocolError("Unexpected native temporary evidence")
            elif name == "thread-writer-locks":
                for child in path.iterdir():
                    if _metadata(child).st_size != 0:
                        raise ProtocolError("Unexpected persistent thread writer evidence")
            continue
        match = _NATIVE_FILE.fullmatch(name)
        if match is None:
            raise ProtocolError("Unexpected native home evidence")
        group, sidecar = match.groups()
        if sidecar in files[group] or (group == "state" and sidecar is not None):
            raise ProtocolError("Ambiguous native databases or state sidecars")
        files[group][sidecar] = path

    if "state" not in files:
        raise ProtocolError("Original native thread index is missing")
    # Only the copied auxiliary files are opened with WAL semantics. Never
    # checkpoint, recreate a sidecar or discard uncheckpointed rows in the original.
    with tempfile.TemporaryDirectory(prefix="milo-native-evidence-") as directory:
        for group, paths in sorted(files.items()):
            base = paths.get(None)
            if (
                base is None
                or any(
                    file.name != f"{base.name}-{suffix}" for suffix, file in paths.items() if suffix
                )
                or (("wal" in paths) != ("shm" in paths))
            ):
                raise ProtocolError("Incomplete native database evidence")
            for file in paths.values():
                data = _read_native(file)
                (Path(directory) / file.name).write_bytes(data)
                digests[file.name] = hashlib.sha256(data).hexdigest()
            _check_database(
                Path(directory) / base.name,
                {"threads"} if group == "state" else _AUXILIARY[group],
                immutable=group == "state",
            )
    return hashlib.sha256(encoded(digests)).hexdigest()

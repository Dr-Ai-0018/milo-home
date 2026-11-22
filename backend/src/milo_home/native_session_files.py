"""Classify dedicated native state and capture bounded SQLite snapshots."""

import os
import re
import sqlite3
import stat
import tempfile
import time
from contextlib import closing
from pathlib import Path

from .database_identity import private_run_directory
from .workspace_archive_files import credential_path, read_file, relative_file, write_file

DATABASE = re.compile(r"(?:state|thread_history|goals|memories|queue|logs)_\d+\.sqlite\Z")
OMITTED = {"config.toml", "newawz.config.toml", "auth.json", "credential.env"}
METADATA = {"installation_id", ".sandbox_migration", "history.jsonl", "session_index.jsonl"}
RECORD_ROOTS = {"sessions", "archived_sessions"}
SQLITE_MAINTENANCE_LOCK = ".sqlite-maintenance.lock"


def verify_sqlite_maintenance_lock(path):
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or metadata.st_size != 0
    ):
        raise ValueError("Unexpected native SQLite maintenance lock")


def make_private_parents(root, name):
    directory = root
    for part in relative_file(name).parts[:-1]:
        directory = directory / part
        directory.mkdir(mode=0o700, exist_ok=True)


def file_kind(name):
    path = relative_file(name)
    if len(path.parts) == 1 and DATABASE.fullmatch(name):
        return "sqlite"
    if name in METADATA:
        return "file"
    if path.parts[0] in RECORD_ROOTS and path.suffix == ".jsonl":
        return "file"
    raise ValueError("Unclassified native state requires an explicit exclusion")


def dedicated_home(home):
    private_run_directory(home)
    active = os.environ.get("CODEX_HOME")
    if home == Path.home() / ".codex" or (active and home == Path(active).resolve()):
        raise ValueError("Only an inactive dedicated native home may be captured")


def inventory(home, exclusions, budget):
    dedicated_home(home)
    excluded = set(exclusions)
    for name in excluded:
        relative_file(name)
        if (
            name.split("/")[0] in RECORD_ROOTS
            or name in METADATA
            or DATABASE.fullmatch(name)
            or (name.endswith(("-wal", "-shm")) and DATABASE.fullmatch(name[:-4]))
        ):
            raise ValueError("Core native state cannot be excluded")
        if not (home / name).exists() or (home / name).resolve() != home / name:
            raise ValueError("Native exclusions must identify existing non-linked paths")
    files = {}
    omitted = set()
    total = 0
    visited = 0

    def walk(directory):
        nonlocal total, visited
        for path in sorted(directory.iterdir()):
            visited += 1
            if visited > 20000:
                raise ValueError("Native inventory exceeds the file count bound")
            name = path.relative_to(home).as_posix()
            if name == SQLITE_MAINTENANCE_LOCK:
                verify_sqlite_maintenance_lock(path)
                omitted.add(name)
                continue
            if name in excluded or name in OMITTED or credential_path(name):
                omitted.add(name)
                continue
            metadata = path.lstat()
            if path.resolve() != path or metadata.st_uid != 0:
                raise ValueError("Native state must be root-owned without links")
            if stat.S_ISDIR(metadata.st_mode):
                walk(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Native state must consist of ordinary files")
            if name.endswith(("-wal", "-shm")) and DATABASE.fullmatch(name[:-4]):
                if not (home / name[:-4]).is_file():
                    raise ValueError("Native SQLite sidecar has no database")
                if name.endswith("-shm"):
                    continue
                kind = "wal"
            else:
                kind = file_kind(name)
            _, fingerprint = read_file(home, name, budget - total)
            total += fingerprint["bytes"]
            files[name] = {"kind": kind, **fingerprint}

    walk(home)
    return {"files": files, "omitted": sorted(omitted), "bytes": total}


def sqlite_snapshot(source, destination, budget, inventory_files):
    # SQLite can create WAL/SHM files even when opening a WAL database in
    # read-only mode. Replay the inventoried bytes away from the source.
    with tempfile.TemporaryDirectory(prefix=".sqlite-input-", dir=destination.parent) as scratch:
        local = Path(scratch) / source.name
        remaining = budget
        for name in (source.name, source.name + "-wal"):
            expected = inventory_files.get(name)
            if expected is None:
                continue
            content, actual = read_file(source.parent, name, remaining)
            if actual != {key: expected[key] for key in actual}:
                raise ValueError("Native SQLite input changed during capture")
            write_file(local.parent / name, content)
            remaining -= len(content)
        _standalone_sqlite(local, destination, budget)


def _standalone_sqlite(source, destination, budget):
    deadline = time.monotonic() + 20
    write_file(destination, b"")

    def progress(status, remaining, pages):
        if time.monotonic() >= deadline or pages * page_size > budget:
            raise ValueError("Native SQLite snapshot exceeds its time or byte budget")

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as reader:
        reader.execute("PRAGMA query_only=ON")
        page_size = reader.execute("PRAGMA page_size").fetchone()[0]
        if reader.execute("PRAGMA page_count").fetchone()[0] * page_size > budget:
            raise ValueError("Native SQLite snapshot exceeds its byte budget")
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer, pages=64, progress=progress, sleep=0.01)
            # Persist a standalone database, never an archive requiring a live WAL.
            writer.execute("PRAGMA journal_mode=DELETE")
    verify_sqlite(destination)
    descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_sqlite(path):
    # Only standalone captured/restored artifacts use immutable mode.
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Native SQLite snapshot failed integrity verification")

"""Private SQLite snapshots, independent of application inspection or migrations."""

import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from .path_policy import PathPolicy


def private_database_file(path: Path, *, policy=None):
    (policy if policy is not None else PathPolicy.configured()).require_external(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise ValueError("Database must be a private regular file")
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("Database directory must be private")


@contextmanager
def readonly_snapshot(path: Path, *, policy=None):
    private_database_file(path, policy=policy)
    uri = f"file:{quote(str(path))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()

"""Keep root coordination separate from a service account's SQLite files."""

import os
import pwd
import threading
from contextlib import contextmanager
from pathlib import Path

from .database import Database
from .path_policy import PathPolicy


class ServiceDatabase(Database):
    def __init__(self, path: Path, account: str):
        if os.geteuid() != 0:
            raise ValueError("Database account selection requires root")
        super().__init__(path)
        try:
            identity = pwd.getpwnam(account)
        except KeyError as error:
            raise ValueError("Database account does not exist") from error
        if identity.pw_uid == 0:
            raise ValueError("Select a non-root database account")
        self.uid = identity.pw_uid
        self.gid = identity.pw_gid
        self._validate_path()

    def _validate_path(self):
        if not self.path.is_absolute() or self.path.parent.resolve() != self.path.parent:
            raise ValueError("Database parent must be an absolute non-linked path")
        parent = self.path.parent.stat()
        if parent.st_uid != self.uid or parent.st_mode & 0o077:
            raise ValueError("Database parent must be private and owned by its account")
        if self.path.is_symlink():
            raise ValueError("Database must not be linked")
        if self.path.exists():
            metadata = self.path.stat()
            if metadata.st_uid != self.uid or metadata.st_mode & 0o077:
                raise ValueError("Database must be private and owned by its account")

    @contextmanager
    def _as_owner(self):
        # Effective identity is process-wide. This adapter is only for the
        # synchronous root management CLI, never the threaded HTTP service.
        if (
            threading.current_thread() is not threading.main_thread()
            or threading.active_count() != 1
        ):
            raise ValueError("Database identity switching requires a single-threaded process")
        self._validate_path()
        original_uid = os.geteuid()
        original_gid = os.getegid()
        if original_uid == self.uid:
            yield
            return
        if original_uid != 0:
            raise ValueError("Cannot switch database identity")
        try:
            os.setegid(self.gid)
            os.seteuid(self.uid)
            yield
        finally:
            os.seteuid(original_uid)
            os.setegid(original_gid)

    def initialize(self):
        with self._as_owner():
            super().initialize()

    @contextmanager
    def connect(self, *, write: bool = False):
        with self._as_owner(), super().connect(write=write) as connection:
            yield connection

    @contextmanager
    def snapshot(self):
        with self._as_owner(), super().snapshot() as connection:
            yield connection


def private_run_directory(path: Path) -> Path:
    PathPolicy.configured().require_external(path, directory=True)
    if os.geteuid() != 0 or not path.is_absolute() or path.resolve() != path:
        raise ValueError("Run directory must be a root-managed external absolute path")
    metadata = path.stat()
    if not path.is_dir() or metadata.st_uid != 0 or metadata.st_mode & 0o077:
        raise ValueError("Run directory must be root-owned and private")
    for parent in path.parents:
        metadata = parent.stat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError("Run directory ancestors must only be writable by root")
    return path

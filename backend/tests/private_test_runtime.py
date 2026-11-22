"""Root-private synthetic runtimes with bounded, identity-checked cleanup."""

import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from milo_home.database_identity import private_run_directory

ROOT = Path.home() / ".cache" / "milo-home-tests"


def runtime_root():
    private_run_directory(ROOT.parent)
    ROOT.mkdir(mode=0o700, exist_ok=True)
    return private_run_directory(ROOT)


def cleanup_runtime(path, identity):
    root = runtime_root()
    if path.parent != root or path.resolve() != path or not path.name.startswith("milo-"):
        raise ValueError("Refusing cleanup outside the synthetic runtime root")
    metadata = path.lstat()
    if (metadata.st_dev, metadata.st_ino) != identity:
        raise ValueError("Synthetic runtime identity changed before cleanup")
    private_run_directory(path)
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("Cleanup requires symlink-safe rmtree")
    shutil.rmtree(path)


@contextmanager
def private_test_runtime(prefix):
    if not re.fullmatch(r"milo-[a-z-]+-", prefix):
        raise ValueError("Invalid synthetic runtime prefix")
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=runtime_root()))
    metadata = path.lstat()
    try:
        yield path
    finally:
        if os.geteuid() != 0:
            raise RuntimeError("Root identity was not restored before runtime cleanup")
        cleanup_runtime(path, (metadata.st_dev, metadata.st_ino))

"""Private, bounded artifacts for an offline workspace recovery rehearsal."""

import hashlib
import json
import os
import shutil
import stat
from pathlib import PurePosixPath

from .database_identity import private_run_directory

DEFAULT_BUDGET = 256 * 1024 * 1024
RESERVE = 64 * 1024 * 1024


def relative_file(name):
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or path.as_posix() != name
        or any(part in {".", "..", ".git"} for part in path.parts)
        or "\x00" in name
    ):
        raise ValueError("Workspace file requires a canonical relative path")
    return path


def credential_path(name):
    path = relative_file(name)
    return (
        any(part in {".codex", ".claude", ".ssh"} for part in path.parts)
        or path.name in {"auth.json", "credential.env"}
        or path.name.endswith(".env")
        or (path.name.startswith(".env") and path.name != ".env.example")
    )


def read_file(root, name, budget):
    relative_file(name)
    path = root / name
    if path.resolve() != path:
        raise ValueError("Workspace files must not traverse symbolic links")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o7000:
        raise ValueError("Workspace backup only supports ordinary files")
    if before.st_size > budget:
        raise ValueError("Workspace file exceeds the byte budget")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        content = stream.read(budget + 1)
        after = os.fstat(stream.fileno())
    if len(content) > budget or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("Workspace file changed during capture")
    return content, {
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "mode": stat.S_IMODE(before.st_mode),
    }


def write_file(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()


def new_directory(path, budget, required):
    if not RESERVE <= budget <= 16 * 1024**3:
        raise ValueError("Workspace budget must be between 64 MiB and 16 GiB")
    private_run_directory(path.parent)
    if path.resolve() != path or path.exists():
        raise ValueError("Workspace output must be a new canonical private directory")
    if required > budget or shutil.disk_usage(path.parent).free < required + RESERVE:
        raise ValueError("Insufficient workspace budget or free disk space")
    path.mkdir(mode=0o700)
    write_file(path / "INCOMPLETE", b"Offline workspace operation incomplete; never execute.\n")


def complete(path):
    (path / "INCOMPLETE").unlink()
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_artifacts(directory, expected_digest, budget):
    private_run_directory(directory)
    if (directory / "INCOMPLETE").exists():
        raise ValueError("Workspace capture is incomplete")
    content, _ = read_file(directory, "manifest.json", budget)
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise ValueError("Workspace manifest differs from the trusted digest")
    manifest = json.loads(content)
    if manifest.get("version") != 1 or manifest.get("kind") != "offline-git-workspace":
        raise ValueError("Unsupported workspace archive")
    total = len(content)
    for name, expected in manifest["artifacts"].items():
        _, actual = read_file(directory, name, budget)
        total += actual["bytes"]
        if actual != expected:
            raise ValueError("Workspace artifact differs from the trusted manifest")
    if total > budget:
        raise ValueError("Workspace artifacts exceed the byte budget")
    return manifest

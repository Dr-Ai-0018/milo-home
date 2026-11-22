"""Bounded snapshots of files explicitly placed in one task's handoff directory."""

import os
import stat
import unicodedata
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID

from .work_artifacts import DIRECTORY_FLAGS, FILE_REFERENCE_FLAGS

MAX_FILES = 16
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TASK_BYTES = 128 * 1024 * 1024


def _display_name(name):
    normalized = unicodedata.normalize("NFKC", name)
    if (
        not 1 <= len(name.encode("utf-8", errors="surrogatepass")) <= 160
        or any(value.startswith(".") or value != value.strip() for value in (name, normalized))
        or any(char in '\\/:*?"<>|' or not char.isprintable() for char in normalized)
    ):
        raise ValueError("Unsafe attachment filename")
    return normalized.casefold()


def _snapshot(directory, name, remaining):
    with ExitStack() as stack:
        reference = os.open(name, FILE_REFERENCE_FLAGS, dir_fd=directory)
        stack.callback(os.close, reference)
        before = os.fstat(reference)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("Attachment must be a standalone regular file")
        if before.st_size > min(MAX_FILE_BYTES, remaining):
            raise ValueError("Attachment exceeds the byte limit")
        fd = os.open(f"/proc/self/fd/{reference}", os.O_RDONLY | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as source:
            data = source.read(before.st_size + 1)
            after = os.fstat(source.fileno())
        if (
            len(data) != before.st_size
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or after.st_nlink != 1
        ):
            raise ValueError("Attachment changed while being read")
        return data


def _workspace(stack, root):
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("Expected an absolute workspace path")
    directory = os.open("/", DIRECTORY_FLAGS)
    stack.callback(os.close, directory)
    for part in root.parts[1:]:
        directory = os.open(part, DIRECTORY_FLAGS, dir_fd=directory)
        stack.callback(os.close, directory)
    return directory


def task_directories(task_id):
    base = f"tasks/{UUID(task_id)}"
    return {"work_path": f"{base}/work", "handoff_path": f"{base}/handoff"}


def prepare_task_directories(root: Path, task_id: str):
    with ExitStack() as stack:
        workspace = _workspace(stack, root)
        try:
            os.mkdir("tasks", mode=0o700, dir_fd=workspace)
        except FileExistsError:
            pass
        tasks = os.open("tasks", DIRECTORY_FLAGS, dir_fd=workspace)
        stack.callback(os.close, tasks)
        name = str(UUID(task_id))
        os.mkdir(name, mode=0o700, dir_fd=tasks)
        task = os.open(name, DIRECTORY_FLAGS, dir_fd=tasks)
        stack.callback(os.close, task)
        for part in ("work", "handoff"):
            os.mkdir(part, mode=0o700, dir_fd=task)


def handoff_files(root: Path, task_id: str):
    """Yield name and bytes; the caller must stage the entire batch before publishing."""
    with ExitStack() as stack:
        directory = _workspace(stack, root)
        for part in ("tasks", str(UUID(task_id)), "handoff"):
            directory = os.open(part, DIRECTORY_FLAGS, dir_fd=directory)
            stack.callback(os.close, directory)
        names = []
        keys = set()
        with os.scandir(directory) as entries:
            for entry in entries:
                if len(names) == MAX_FILES:
                    raise ValueError("Too many attachment files")
                key = _display_name(entry.name)
                if key in keys:
                    raise ValueError("Conflicting attachment filenames")
                keys.add(key)
                names.append(entry.name)
        remaining = MAX_TASK_BYTES
        for name in sorted(names):
            data = _snapshot(directory, name, remaining)
            remaining -= len(data)
            yield name, data

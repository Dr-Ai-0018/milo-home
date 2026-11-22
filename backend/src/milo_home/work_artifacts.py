"""Explicit local publication; the web side can only read saved snapshots."""

import hashlib
import os
import re
import stat
import time
from contextlib import ExitStack
from pathlib import Path

from .errors import ApiError
from .work_access import require_task

MAX_BYTES = 16_384
MAX_FILES = 8
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_REFERENCE_FLAGS = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC


def read_snapshot(root: Path, path: str):
    parts = path.split("/")
    if (
        not root.is_absolute()
        or ".." in root.parts
        or not 1 <= len(path) <= 240
        or any(
            not part
            or part.startswith(".")
            or any(char in "\\:" or not char.isprintable() for char in part)
            for part in parts
        )
        or not parts[-1].endswith(".txt")
    ):
        raise ValueError("Expected a non-hidden relative .txt path")

    with ExitStack() as stack:
        # Open every directory by descriptor; a symlink or concurrent rename
        # cannot redirect an open through a path resolved earlier.
        directory = os.open("/", DIRECTORY_FLAGS)
        stack.callback(os.close, directory)
        for part in root.parts[1:] + tuple(parts[:-1]):
            directory = os.open(part, DIRECTORY_FLAGS, dir_fd=directory)
            stack.callback(os.close, directory)
        reference = os.open(parts[-1], FILE_REFERENCE_FLAGS, dir_fd=directory)
        stack.callback(os.close, reference)
        before = os.fstat(reference)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
            raise ValueError("Expected a small standalone regular file")
        # Reopen this checked inode; opening the path again could open a
        # replacement device or FIFO before we get to inspect its type.
        fd = os.open(f"/proc/self/fd/{reference}", os.O_RDONLY | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as file:
            data = file.read(MAX_BYTES + 1)
            after = os.fstat(file.fileno())
    if (
        len(data) != before.st_size
        or after.st_nlink != 1
        or after.st_size != before.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise ValueError("File changed during verification")
    text = data.decode("utf-8")
    if any(not char.isprintable() and char not in "\t\r\n" for char in text):
        raise ValueError("Expected plain UTF-8 text")
    return text, hashlib.sha256(data).hexdigest()


def list_artifacts(database, owner: str, task_id: str):
    with database.connect() as connection:
        connection.execute("BEGIN")
        require_task(connection, owner, task_id)
        rows = connection.execute(
            "SELECT path,text,sha256,checked_at FROM work_artifacts "
            "WHERE task_id=? ORDER BY checked_at,path",
            (task_id,),
        ).fetchall()
    return {"artifacts": [dict(row) for row in rows]}


def ready_workspace(connection, owner: str, task_id: str) -> Path:
    task = require_task(connection, owner, task_id)
    if (
        task["status"] != "completed"
        or connection.execute(
            "SELECT 1 FROM execution_windows WHERE task_id=? AND state!='closed'", (task_id,)
        ).fetchone()
    ):
        raise ApiError(409, "work_artifact_not_ready", "等交办结束并收尾后再登记成果。")
    row = connection.execute(
        "SELECT root_path FROM workspaces WHERE id=?", (task["workspace_id"],)
    ).fetchone()
    return Path(row["root_path"])


def save_snapshot(connection, task_id, path, text, digest, checked_at):
    rows = connection.execute(
        "SELECT path,sha256,checked_at FROM work_artifacts WHERE task_id=?", (task_id,)
    ).fetchall()
    for row in rows:
        if row["path"] != path:
            continue
        if row["sha256"] != digest:
            raise ApiError(409, "work_artifact_exists", "这个位置已有核对记录。")
        return {"checked_at": row["checked_at"], "sha256": digest, "created": False}
    if len(rows) >= MAX_FILES:
        raise ApiError(409, "work_artifact_limit", "这张单的成果记录已满。")
    connection.execute(
        "INSERT INTO work_artifacts (task_id,path,text,sha256,checked_at) VALUES (?,?,?,?,?)",
        (task_id, path, text, digest, checked_at),
    )
    return {"checked_at": checked_at, "sha256": digest, "created": True}


def publish_artifact(database, owner: str, task_id: str, path: str, expected_sha256: str):
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Expected a reviewed SHA-256")
    with database.connect() as connection:
        connection.execute("BEGIN")
        root = ready_workspace(connection, owner, task_id)
    # ServiceDatabase temporarily uses the database owner's UID. Workspace
    # files are opened under the local operator's UID, outside that context.
    text, digest = read_snapshot(root, path)
    checked_at = int(time.time())
    if digest != expected_sha256:
        raise ApiError(409, "work_artifact_changed", "文件与预先核对的内容不一致。")
    with database.connect(write=True) as connection:
        if ready_workspace(connection, owner, task_id) != root:
            raise ApiError(409, "work_artifact_changed", "工作桌登记已变化。")
        return save_snapshot(connection, task_id, path, text, digest, checked_at)

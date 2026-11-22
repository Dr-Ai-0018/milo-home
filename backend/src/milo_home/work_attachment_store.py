"""Private, bounded file storage; only the business index exposes a sealed batch."""

import hashlib
import os
import stat
from contextlib import contextmanager
from fcntl import LOCK_EX, flock
from uuid import UUID, uuid4

from .work_artifacts import DIRECTORY_FLAGS
from .work_attachment_source import MAX_FILE_BYTES


def _open_directory(path):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Expected an absolute attachment root")
    fd = os.open("/", DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        if os.fstat(fd).st_mode & 0o077:
            raise ValueError("Attachment root must be private")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _subdirectory(parent, name, *, create, owner):
    created = False
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            created = True
    fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    try:
        if create and created:
            _assign_owner(fd, owner)
            os.fsync(fd)
            os.fsync(parent)
        metadata = os.fstat(fd)
        if metadata.st_uid != owner or metadata.st_mode & 0o077:
            raise ValueError("Attachment directory must be private")
        yield fd
    finally:
        os.close(fd)


def _assign_owner(fd, owner):
    if os.fstat(fd).st_uid != owner:
        os.fchown(fd, owner, -1)


def _read(directory, name, size, digest, owner):
    if not 0 <= size <= MAX_FILE_BYTES:
        raise ValueError("Sealed attachment exceeds the read bound")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != owner
            or stat.S_IMODE(before.st_mode) != 0o400
            or before.st_size != size
        ):
            raise ValueError("Sealed attachment metadata differs")
        data = source.read(size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Sealed attachment digest differs")
    return data


def _used(tasks, owner):
    total = 0
    with os.scandir(tasks) as entries:
        for task in entries:
            if str(UUID(task.name)) != task.name:
                raise ValueError("Invalid attachment task directory")
            with _subdirectory(tasks, task.name, create=False, owner=owner) as directory:
                with os.scandir(directory) as files:
                    for item in files:
                        info = item.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
                            raise ValueError("Invalid entry in the attachment store")
                        total += info.st_size
    return total


def _remove_pending(task):
    with os.scandir(task) as entries:
        for item in entries:
            if item.name.startswith(".pending-"):
                os.unlink(item.name, dir_fd=task)
    os.fsync(task)


def _write(task, name, data, owner):
    temp = ".pending-" + uuid4().hex
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=task)
    try:
        with os.fdopen(fd, "wb") as output:
            _assign_owner(output.fileno(), owner)
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), 0o400)
            os.fsync(output.fileno())
        os.link(temp, name, src_dir_fd=task, dst_dir_fd=task, follow_symlinks=False)
    finally:
        os.unlink(temp, dir_fd=task)


class AttachmentStore:
    def __init__(self, root, *, max_bytes, owner=None):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("Attachment capacity must be a positive byte count")
        self.root = root
        self.max_bytes = max_bytes
        self.owner = os.geteuid() if owner is None else owner

    def check_workspace(self, workspace):
        store_path = self.root.resolve()
        work_path = workspace.resolve()
        if store_path.is_relative_to(work_path) or work_path.is_relative_to(store_path):
            raise ValueError("Attachment store and working directory must not overlap")

    @contextmanager
    def _task(self, task_id, *, create):
        root = _open_directory(self.root)
        try:
            if os.fstat(root).st_uid != self.owner:
                raise ValueError("Attachment root must belong to the service account")
            if create:
                flock(root, LOCK_EX)
            with _subdirectory(root, "tasks", create=create, owner=self.owner) as tasks:
                with _subdirectory(
                    tasks, str(UUID(task_id)), create=create, owner=self.owner
                ) as task:
                    yield tasks, task
        finally:
            os.close(root)

    def read(self, task_id, item):
        with self._task(task_id, create=False) as (_, task):
            return _read(
                task, str(UUID(item["id"])), item["size_bytes"], item["sha256"], self.owner
            )

    def seal(self, task_id, items):
        with self._task(task_id, create=True) as (tasks, task):
            _remove_pending(task)
            remaining = self.max_bytes - _used(tasks, self.owner)
            stored = []
            created = []
            try:
                for item, data in items:
                    name = str(UUID(item["id"]))
                    if self._seal_one(task, name, item, data, remaining):
                        created.append(name)
                        remaining -= len(data)
                    stored.append(item)
            except (OSError, ValueError):
                for name in created:
                    os.unlink(name, dir_fd=task)
                os.fsync(task)
                raise
            os.fsync(task)
            return stored

    def _seal_one(self, task, name, item, data, remaining):
        try:
            _read(task, name, item["size_bytes"], item["sha256"], self.owner)
            return False
        except FileNotFoundError:
            pass
        disk = os.fstatvfs(task)
        if len(data) > remaining or len(data) > disk.f_bavail * disk.f_frsize:
            raise ValueError("Attachment store capacity exceeded")
        _write(task, name, data, self.owner)
        return True

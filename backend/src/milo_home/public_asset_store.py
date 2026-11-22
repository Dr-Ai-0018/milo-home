"""Explicit source snapshots and bounded, immutable public asset bytes.

The store knows IDs and bytes, never publication state or private task paths.
Unregistered and interrupted files are retained and charged to its budget.
"""

import hashlib
import os
import stat
import struct
import unicodedata
import zlib
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, flock
from uuid import UUID, uuid4

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_REVISION_BYTES = 128 * 1024 * 1024
MAX_PNG_BYTES = 8 * 1024 * 1024
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def display_filename(name):
    normalized = unicodedata.normalize("NFKC", name)
    if (
        not 1 <= len(name.encode("utf-8", errors="surrogatepass")) <= 160
        or any(v.startswith(".") or v != v.strip() for v in (name, normalized))
        or any(c in '\\/:*?"<>|' or not c.isprintable() for c in normalized)
    ):
        raise ValueError("Unsafe public filename")
    return name


def _directory(path):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Expected an absolute, non-linked directory")
    fd = os.open("/", DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def source_snapshot(path):
    """Open checked inode, not a second potentially replaced path."""
    display_filename(path.name)
    directory = _directory(path.parent)
    try:
        reference = os.open(path.name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        before = os.fstat(reference)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAX_FILE_BYTES
        ):
            raise ValueError("Expected a bounded standalone regular file")
        fd = os.open(f"/proc/self/fd/{reference}", os.O_RDONLY | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as source:
            data = source.read(before.st_size + 1)
            after = os.fstat(source.fileno())
        if (
            len(data) != before.st_size
            or after.st_size != before.st_size
            or after.st_nlink != 1
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise ValueError("Source changed while reading")
        return data
    finally:
        os.close(reference)


def png_dimensions(data):
    if (
        not 33 <= len(data) <= MAX_PNG_BYTES
        or data[:8] != b"\x89PNG\r\n\x1a\n"
        or data[8:16] != b"\x00\x00\x00\x0dIHDR"
        or zlib.crc32(data[12:29]) != struct.unpack(">I", data[29:33])[0]
    ):
        raise ValueError("Expected PNG signature and valid IHDR")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", data[16:29]
    )
    depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if (
        not width
        or not height
        or width * height > 16000000
        or depth not in depths.get(color, set())
        or compression != 0
        or filtering != 0
        or interlace not in {0, 1}
    ):
        raise ValueError("Unsupported PNG dimensions or IHDR")
    return width, height


class PublicAssetStore:
    def __init__(self, root, *, max_bytes, owner=None):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("Public assets require an explicit positive byte budget")
        self.root, self.max_bytes = root, max_bytes
        self.owner = os.geteuid() if owner is None else owner

    def check_workspace(self, workspace):
        root, workspace = self.root.resolve(), workspace.resolve()
        if root.is_relative_to(workspace) or workspace.is_relative_to(root):
            raise ValueError("Public asset store and working directory must not overlap")

    def _private(self, fd):
        info = os.fstat(fd)
        if info.st_uid != self.owner or info.st_mode & 0o077:
            raise ValueError("Public asset directories must be private and service-owned")

    @contextmanager
    def _child(self, parent, name, create):
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            else:
                fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
                try:
                    os.fchown(fd, self.owner, -1)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.fsync(parent)
        fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
        try:
            self._private(fd)
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _work(self, work_id, *, create=False):
        root = _directory(self.root)
        try:
            self._private(root)
            if create:
                flock(root, LOCK_EX | LOCK_NB)
            with self._child(root, "works", create) as works:
                with self._child(works, str(UUID(work_id)), create) as work:
                    yield works, work
        finally:
            os.close(root)

    def _read(self, directory, item):
        size = item["size_bytes"]
        if not 0 <= size <= MAX_FILE_BYTES:
            raise ValueError("Public asset exceeds the read bound")
        fd = os.open(
            str(UUID(item["id"])), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != self.owner
                or stat.S_IMODE(info.st_mode) != 0o400
                or info.st_size != size
            ):
                raise ValueError("Public asset metadata differs")
            data = source.read(size + 1)
        if len(data) != size or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Public asset digest differs")
        return data

    def read(self, work_id, item):
        with self._work(work_id) as (_, directory):
            return self._read(directory, item)

    def _used(self, works):
        total = 0
        with os.scandir(works) as entries:
            for entry in entries:
                if str(UUID(entry.name)) != entry.name:
                    raise ValueError("Unexpected public asset directory")
                with self._child(works, entry.name, False) as directory:
                    with os.scandir(directory) as items:
                        for item in items:
                            info = item.stat(follow_symlinks=False)
                            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner:
                                raise ValueError("Unexpected public asset file")
                            total += info.st_size
        return total

    def seal(self, work_id, item, data):
        if len(data) != item["size_bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Public asset bytes differ from metadata")
        with self._work(work_id, create=True) as (works, directory):
            try:
                self._read(directory, item)
                return
            except FileNotFoundError:
                pass
            disk = os.fstatvfs(directory)
            if (
                len(data) > self.max_bytes - self._used(works)
                or len(data) > disk.f_bavail * disk.f_frsize
                or len(data) > MAX_FILE_BYTES
            ):
                raise ValueError("Public asset capacity exceeded")
            pending = ".pending-" + uuid4().hex
            fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as output:
                os.fchown(output.fileno(), self.owner, -1)
                output.write(data)
                output.flush()
                os.fchmod(output.fileno(), 0o400)
                os.fsync(output.fileno())
            os.link(
                pending,
                str(UUID(item["id"])),
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
            os.unlink(pending, dir_fd=directory)
            os.fsync(directory)

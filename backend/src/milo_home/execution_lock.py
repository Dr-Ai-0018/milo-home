"""Host-wide, directory-independent arbitration for supported execution entries."""

import errno
import hashlib
import os
import socket
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import ApiError


class ExecutionIdentityError(RuntimeError):
    """The held database can no longer be used; leave business occupancy intact."""


def _host_namespace():
    if os.geteuid() != 0:
        raise ValueError("Execution coordination requires root")
    own = os.stat("/proc/self/ns/net")
    host = os.stat("/proc/1/ns/net")
    if (own.st_dev, own.st_ino) != (host.st_dev, host.st_ino):
        raise ExecutionIdentityError("Execution coordination requires the host network namespace")


def _validate_file(metadata, owner):
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != owner or metadata.st_mode & 0o077:
        raise ExecutionIdentityError(
            "Execution database must be a private ordinary file of its account"
        )


def _address(kind: str, value: bytes) -> bytes:
    digest = hashlib.sha256(value).hexdigest().encode()
    return b"\x00milo-home-execution-v1-" + kind.encode() + b"-" + digest


@dataclass
class ExecutionLease:
    source: Path
    canonical: Path
    descriptor: int
    owner: int
    pid: int
    active: bool = True

    def verify(self):
        """Fail closed if the database, namespace or owning process changed."""
        try:
            self._verify()
        except (OSError, ValueError) as error:
            raise ExecutionIdentityError(
                "Execution database or host identity unavailable"
            ) from error

    def _verify(self):
        _host_namespace()
        if not self.active or os.getpid() != self.pid:
            raise ExecutionIdentityError("Execution lease is no longer owned by this process")
        if self.source.resolve(strict=True) != self.canonical:
            raise ExecutionIdentityError("Execution database path changed")
        current = self.canonical.stat(follow_symlinks=False)
        held = os.fstat(self.descriptor)
        _validate_file(current, self.owner)
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise ExecutionIdentityError("Execution database identity changed")
        parent = self.canonical.parent.stat()
        if parent.st_uid != self.owner or parent.st_mode & 0o077:
            raise ExecutionIdentityError(
                "Execution database directory must be private and account-owned"
            )


@contextmanager
def execution_lock(database):
    """Hold both path and inode names without a web-account-owned lock file.

    This is arbitration, not authorization or proof that an old family stopped.
    Kernel socket references are never inherited across exec. A fork without
    exec conservatively retains occupancy until every reference is closed.
    """
    _host_namespace()
    source = database.path.absolute()
    canonical = source.resolve(strict=True)
    owner = getattr(database, "uid", os.geteuid())
    with ExitStack() as stack:
        descriptor = os.open(canonical, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        stack.callback(os.close, descriptor)
        metadata = os.fstat(descriptor)
        _validate_file(metadata, owner)
        lease = ExecutionLease(source, canonical, descriptor, owner, os.getpid())
        lease.verify()
        addresses = [
            _address("path", os.fsencode(canonical)),
            _address("inode", f"{metadata.st_dev}:{metadata.st_ino}".encode()),
        ]
        for address in addresses:
            marker = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
            marker.set_inheritable(False)
            try:
                marker.bind(address)
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
                raise ApiError(409, "worker_busy", "执行协调进程正在运行。") from error
        lease.verify()
        try:
            yield lease
        finally:
            lease.active = False

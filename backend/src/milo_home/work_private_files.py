"""Publish complete immutable private files without overwriting evidence."""

import os
from uuid import uuid4

from .codex_config import private_read
from .codex_rpc import ProtocolError


def sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_file(path, content):
    temporary = path.parent / (".main-init-" + uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if private_read(path) != content:
                raise ProtocolError("Original main registration or configuration differs") from None
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)

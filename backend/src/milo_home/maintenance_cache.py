"""Bounded reads and atomic publication of a root-owned, API-readable summary."""

import os
import stat
import tempfile
from pathlib import Path

from .maintenance_models import MaintenanceDocument, age_summary, unknown_summary

MAX_DOCUMENT_BYTES = 64 * 1024


def checked_parent(path: Path):
    parent = path.parent.stat()
    if (
        not path.is_absolute()
        or path.resolve() != path
        or parent.st_uid != 0
        or parent.st_mode & 0o022
    ):
        raise ValueError("Maintenance summary requires a protected root-owned directory")


def read_summary(settings, database: Path, *, now: int):
    checked_parent(settings.file)
    try:
        descriptor = os.open(settings.file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return unknown_summary(now)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_DOCUMENT_BYTES
        ):
            raise ValueError("Maintenance summary is not a trusted ordinary file")
        document = MaintenanceDocument.model_validate_json(stream.read(MAX_DOCUMENT_BYTES + 1))
    identity = database.stat()
    if document.owner_id != settings.owner or (
        document.database_device,
        document.database_inode,
    ) != (identity.st_dev, identity.st_ino):
        return unknown_summary(now)
    return age_summary(document.summary, now)


def publish_summary(path: Path, document: MaintenanceDocument):
    if os.geteuid() != 0:
        raise ValueError("Only the maintenance collector can publish observations")
    checked_parent(path)
    content = (document.model_dump_json(indent=2) + "\n").encode()
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("Maintenance summary exceeds the bounded file size")
    descriptor, temporary = tempfile.mkstemp(prefix=".summary-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchown(stream.fileno(), 0, path.parent.stat().st_gid)
            os.fchmod(stream.fileno(), 0o640)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)

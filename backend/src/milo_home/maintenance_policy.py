"""Explicit, root-managed selection and byte policy for paired maintenance."""

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .workspace_archive_files import RESERVE, credential_path, json_bytes, relative_file

CANDIDATE_BUDGET = 4 * 1024**3
POLICY_LIMIT = 64 * 1024


def trusted_bytes(path: Path, *, limit: int) -> bytes:
    """Only read a bounded ordinary root-owned file through protected ancestors."""
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Maintenance evidence requires a canonical absolute file")
    for parent in path.parents:
        metadata = parent.stat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError("Maintenance evidence ancestors must only be writable by root")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_mode & 0o022
            or before.st_size > limit
        ):
            raise ValueError("Maintenance evidence must be a bounded root-managed ordinary file")
        content = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(content) > limit or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("Maintenance evidence changed while being read")
    return content


@dataclass(frozen=True)
class BackupPolicy:
    budget_bytes: int
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    native_exclude: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.budget_bytes) is not int or not RESERVE <= self.budget_bytes <= 16 * 1024**3:
            raise ValueError("Maintenance budget must explicitly select 64 MiB through 16 GiB")
        for field in ("include", "exclude", "native_exclude"):
            names = getattr(self, field)
            if not isinstance(names, tuple) or len(names) > 4096:
                raise ValueError("Maintenance selections require bounded tuples")
            if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
                raise ValueError("Maintenance selection contains ambiguous names")
            for name in names:
                relative_file(name)
        if set(self.include) & set(self.exclude):
            raise ValueError("Maintenance include and exclude decisions overlap")
        if any(credential_path(name) for name in self.include):
            raise ValueError("Maintenance selection cannot include credentials")

    def paired_options(self):
        return {
            "budget": self.budget_bytes,
            "include": self.include,
            "exclude": self.exclude,
            "native_exclude": self.native_exclude,
        }

    @property
    def sha256(self):
        return hashlib.sha256(json_bytes({"version": 1, **asdict(self)})).hexdigest()


def load_backup_policy(path: Path) -> BackupPolicy:
    """A missing budget is an error; the candidate is never silently substituted."""
    value = json.loads(trusted_bytes(path, limit=POLICY_LIMIT))
    required = {"version", "budget_bytes", "include", "exclude", "native_exclude"}
    if (
        not isinstance(value, dict)
        or set(value) != required
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ValueError("Unsupported maintenance backup policy")
    if any(not isinstance(value[name], list) for name in required - {"version", "budget_bytes"}):
        raise ValueError("Maintenance classification requires explicit lists")
    return BackupPolicy(
        budget_bytes=value["budget_bytes"],
        include=tuple(value["include"]),
        exclude=tuple(value["exclude"]),
        native_exclude=tuple(value["native_exclude"]),
    )

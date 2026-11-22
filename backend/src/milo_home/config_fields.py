"""Supported deployment fields and strict, source-aware value conversion."""

import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True)
class Field:
    environment: str
    kind: str
    default: object = None


FIELDS = {
    "app.database": Field("MILO_DATABASE", "path"),
    "app.origins": Field("MILO_ORIGINS", "strings", ("https://localhost",)),
    "app.secure_cookie": Field("MILO_COOKIE_SECURE", "bool", True),
    "app.session_seconds": Field("MILO_SESSION_SECONDS", "positive", 7 * 24 * 3600),
    "app.auth_attempts": Field("MILO_AUTH_ATTEMPTS", "positive", 10),
    "app.auth_window_seconds": Field("MILO_AUTH_WINDOW_SECONDS", "positive", 300),
    "app.body_limit": Field("MILO_BODY_LIMIT", "positive", 16 * 1024),
    "work_attachments.root": Field("MILO_WORK_ATTACHMENT_ROOT", "path"),
    "work_attachments.max_bytes": Field("MILO_WORK_ATTACHMENT_MAX_BYTES", "positive"),
    "public_assets.root": Field("MILO_PUBLIC_ASSET_ROOT", "path"),
    "public_assets.max_bytes": Field("MILO_PUBLIC_ASSET_MAX_BYTES", "positive"),
    "maintenance.file": Field("MILO_MAINTENANCE_FILE", "path"),
    "maintenance.owner": Field("MILO_MAINTENANCE_OWNER", "uuid"),
    "maintenance.backup_directory": Field("MILO_BACKUP_DIRECTORY", "path"),
    "maintenance.development_directory": Field("MILO_DEVELOPMENT_DIRECTORY", "path"),
    "maintenance.success_file": Field("MILO_BACKUP_SUCCESS_FILE", "path"),
    "maintenance.daily_unit": Field("MILO_BACKUP_UNIT", "unit"),
    "execution.database_user": Field("MILO_DATABASE_USER", "string"),
    "execution.run_directory": Field("MILO_WORK_DIRECTORY", "path"),
    "execution.workspace": Field("MILO_WORKSPACE", "path"),
    "execution.reply_directory": Field("MILO_REPLY_DIRECTORY", "path"),
    "execution.profile": Field("MILO_CODEX_PROFILE", "path"),
    "execution.credential_file": Field("MILO_CREDENTIAL_FILE", "path"),
    "execution.cli": Field("MILO_CODEX_CLI", "path"),
    "execution.tool_host": Field("MILO_CODEX_TOOL_HOST", "path"),
    "paths.code_root": Field("MILO_CODE_ROOT", "path"),
    "paths.static_root": Field("MILO_STATIC_ROOT", "path"),
    "paths.protected_roots": Field("MILO_PROTECTED_ROOTS", "paths", ()),
}


def state_database(environment):
    """Use a user-owned, cwd-independent default; never create it while loading."""
    configured = environment.get("XDG_STATE_HOME")
    root = (
        convert("path", configured, environment=True)
        if configured is not None
        else (Path.home() / ".local" / "state")
    )
    return root / "milo-home" / "home.sqlite3"


def convert(kind, value, *, environment=False):
    if environment and not isinstance(value, str):
        raise ValueError("Environment values must be strings")
    if kind == "positive":
        if environment:
            if not value.isascii() or not value.isdecimal():
                raise ValueError("Positive integer required")
            value = int(value)
        if type(value) is not int or value <= 0:
            raise ValueError("Positive integer required")
        return value
    if kind == "bool":
        if environment:
            if value not in {"0", "1"}:
                raise ValueError("Boolean environment flags must be 0 or 1")
            return value == "1"
        if type(value) is not bool:
            raise ValueError("Boolean required")
        return value
    if kind == "strings":
        if environment:
            value = [part.strip() for part in value.split(",")]
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("Nonempty string list required")
        return tuple(convert("string", item) for item in value)
    if kind == "paths":
        if environment:
            value = value.split(":")
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("Nonempty absolute path list required")
        return tuple(convert("path", item) for item in value)
    if kind == "path" and isinstance(value, Path) and not environment:
        value = str(value)
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError("Nonempty value without control characters required")
    if kind == "path":
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("Absolute path required")
        return path
    if kind == "uuid":
        return UUID(value)
    if kind == "unit":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,200}\.(service|timer)", value):
            raise ValueError("Explicit systemd service or timer name required")
        return value
    if kind == "string":
        return value
    raise ValueError("Unsupported configuration field type")

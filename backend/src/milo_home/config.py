from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from .config_loader import load_configuration
from .path_policy import PathPolicy


@dataclass(frozen=True)
class MaintenanceSettings:
    file: Path
    owner: UUID

    def __post_init__(self):
        if not self.file.is_absolute() or self.file.resolve() != self.file:
            raise ValueError("Maintenance summary requires a canonical absolute path")

    @classmethod
    def from_env(cls):
        values = load_configuration().group("maintenance", ("file", "owner"))
        return cls(**values) if values else None


@dataclass(frozen=True)
class WorkAttachmentSettings:
    root: Path
    max_bytes: int

    def __post_init__(self):
        if not self.root.is_absolute() or self.root.resolve() != self.root or self.max_bytes <= 0:
            raise ValueError("Attachments require a canonical private root and positive capacity")

    @classmethod
    def from_env(cls):
        values = load_configuration().group("work_attachments", ("root", "max_bytes"))
        return cls(**values) if values else None


@dataclass(frozen=True)
class PublicAssetSettings:
    root: Path
    max_bytes: int

    def __post_init__(self):
        if not self.root.is_absolute() or self.root.resolve() != self.root or self.max_bytes <= 0:
            raise ValueError(
                "Public assets require a canonical root and explicit positive capacity"
            )

    @classmethod
    def from_env(cls):
        values = load_configuration().group("public_assets", ("root", "max_bytes"))
        return cls(**values) if values else None


@dataclass(frozen=True)
class Settings:
    database: Path
    origins: tuple[str, ...]
    secure_cookie: bool = True
    session_seconds: int = 7 * 24 * 3600
    auth_attempts: int = 10
    auth_window_seconds: int = 300
    body_limit: int = 16 * 1024
    work_attachments: WorkAttachmentSettings | None = None
    maintenance: MaintenanceSettings | None = None
    public_assets: PublicAssetSettings | None = None
    paths: PathPolicy = field(default_factory=PathPolicy)

    def __post_init__(self):
        if not self.database.is_absolute():
            raise ValueError("Database path must be absolute")
        self.paths.require_external(self.database)
        for store in (self.work_attachments, self.public_assets):
            if store:
                self.paths.require_external(store.root, directory=True)
        if self.maintenance:
            self.paths.require_external(self.maintenance.file)
        if not self.origins:
            raise ValueError("At least one exact origin is required")
        for origin in self.origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or origin != f"{parsed.scheme}://{parsed.netloc}"
            ):
                raise ValueError("Origin must be an exact HTTP(S) origin")
            if not self.secure_cookie and (
                parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError("Insecure cookies are limited to explicit local HTTP origins")
        if any(
            value <= 0
            for value in (
                self.session_seconds,
                self.auth_attempts,
                self.auth_window_seconds,
                self.body_limit,
            )
        ):
            raise ValueError("Session and authentication limits must be positive")
        if self.public_assets:
            root = self.public_assets.root
            other_roots = [self.database]
            if self.work_attachments:
                other_roots.append(self.work_attachments.root)
            if any(
                root.is_relative_to(other) or other.is_relative_to(root) for other in other_roots
            ):
                raise ValueError(
                    "Public assets must be separate from database, attachments and web"
                )

    @property
    def hosts(self) -> list[str]:
        return list(dict.fromkeys(urlsplit(origin).hostname for origin in self.origins))

    @classmethod
    def from_env(cls, config_file=None) -> "Settings":
        return cls.from_configuration(load_configuration(config_file))

    @classmethod
    def from_configuration(cls, configuration) -> "Settings":
        attachments = configuration.group("work_attachments", ("root", "max_bytes"))
        maintenance = configuration.group("maintenance", ("file", "owner"))
        public = configuration.group("public_assets", ("root", "max_bytes"))
        return cls(
            **{
                name: configuration.get(f"app.{name}")
                for name in (
                    "database",
                    "origins",
                    "secure_cookie",
                    "session_seconds",
                    "auth_attempts",
                    "auth_window_seconds",
                    "body_limit",
                )
            },
            work_attachments=WorkAttachmentSettings(**attachments) if attachments else None,
            maintenance=MaintenanceSettings(**maintenance) if maintenance else None,
            public_assets=PublicAssetSettings(**public) if public else None,
            paths=PathPolicy.from_configuration(configuration),
        )

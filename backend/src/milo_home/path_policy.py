"""Deployment roots for private data, independent of database or runtime machinery."""

from dataclasses import dataclass
from pathlib import Path

from .config_loader import load_configuration

_PACKAGE_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class PathPolicy:
    code_root: Path | None = None
    static_root: Path | None = None
    protected_roots: tuple[Path, ...] = ()

    def __post_init__(self):
        if not isinstance(self.protected_roots, tuple):
            raise ValueError("Protected roots require an explicit tuple")
        for root in self.roots:
            if not root.is_absolute() or ".." in root.parts:
                raise ValueError("Protected roots must be canonical absolute paths")
            try:
                if root.resolve() != root:
                    raise ValueError("Protected roots must be canonical absolute paths")
                if root.exists() and not root.is_dir():
                    raise ValueError("Protected roots must refer to directories")
            except PermissionError:
                # An exclusion is a declared boundary, not a resource the API
                # must traverse. Privileged deployment preflight resolves it.
                pass
        if self.code_root is not None and not self.package_root.is_relative_to(self.code_root):
            raise ValueError("Configured code root must contain the installed package")

    @property
    def package_root(self):
        return _PACKAGE_ROOT

    @property
    def roots(self):
        roots = [self.package_root, *self.protected_roots]
        if self.code_root is not None:
            roots.append(self.code_root)
        if self.static_root is not None:
            roots.append(self.static_root)
        return tuple(dict.fromkeys(roots))

    def require_external(self, path: Path, *, directory=False):
        if not path.is_absolute():
            raise ValueError("A private data path must be absolute")
        if any(
            path.is_relative_to(root) or (directory and root.is_relative_to(path))
            for root in self.roots
        ):
            raise ValueError("Private data must stay outside code, static and protected roots")
        resolved = path.resolve()
        if any(
            resolved.is_relative_to(root) or (directory and root.is_relative_to(resolved))
            for root in self.roots
        ):
            raise ValueError("Private data must stay outside code, static and protected roots")
        return path

    @classmethod
    def from_configuration(cls, configuration):
        return cls(
            **{
                name: configuration.get(f"paths.{name}")
                for name in ("code_root", "static_root", "protected_roots")
            }
        )

    @classmethod
    def configured(cls):
        # Offline recovery still enforces relevant roots, without reading model,
        # authentication or business settings from unrelated environment fields.
        return cls.from_configuration(load_configuration(sections={"paths"}))

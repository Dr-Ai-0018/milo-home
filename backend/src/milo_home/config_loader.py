"""One explicit TOML/environment resolver shared by API, CLI and child processes."""

import os
import stat
import tomllib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from .config_fields import FIELDS, convert, state_database

_SELECTION = ContextVar("milo_deployment_selection", default=(None, {}))
MAX_CONFIG_BYTES = 128 * 1024


class ConfigurationError(ValueError):
    """Contains field names or fixed messages, never submitted values or paths."""


def selected_path(path, environment):
    value = path if path is not None else environment.get("MILO_CONFIG")
    if value is None:
        return None
    if not str(value).strip() or any(ord(c) < 32 for c in str(value)):
        raise ConfigurationError("Invalid configuration file selection")
    return Path(value).resolve()


def read_file(path):
    """Application config is non-secret: root-owned service-readable files work."""
    if path is None:
        return {}
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o022
                or metadata.st_uid not in {0, os.geteuid()}
                or metadata.st_size > MAX_CONFIG_BYTES
            ):
                raise ConfigurationError("Configuration file failed ownership or size checks")
            content = stream.read(MAX_CONFIG_BYTES + 1)
        if len(content) > MAX_CONFIG_BYTES:
            raise ConfigurationError("Configuration file is too large")
        document = tomllib.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ConfigurationError("Configuration file could not be read or validated") from None
    values = {}
    sections = {key.split(".")[0] for key in FIELDS}
    for section, entries in document.items():
        if section not in sections or not isinstance(entries, dict):
            raise ConfigurationError("Unknown configuration section or invalid table")
        for name, value in entries.items():
            key = f"{section}.{name}"
            if key not in FIELDS:
                raise ConfigurationError("Unknown configuration field")
            values[key] = value
    return values


@dataclass(frozen=True)
class Configuration:
    path: Path | None
    values: dict
    sources: dict
    environment: dict = field(repr=False)

    def get(self, key):
        if key not in FIELDS:
            raise KeyError(key)
        if key in self.values:
            return self.values[key]
        if key == "app.database":
            return state_database(self.environment)
        return FIELDS[key].default

    def group(self, section, names):
        values = {name: self.get(f"{section}.{name}") for name in names}
        if all(value is None for value in values.values()):
            return None
        if any(value is None for value in values.values()):
            raise ConfigurationError(f"Incomplete {section} configuration")
        return values

    def source_report(self):
        return {key: self.sources.get(key, "default") for key in FIELDS}


def load_configuration(path=None, *, environment=None, overrides=None, sections=None):
    """Validate selected sections; full loads reject every malformed explicit input."""
    active_path, active_overrides = _SELECTION.get()
    environment = dict(os.environ) if environment is None else dict(environment)
    path = selected_path(path if path is not None else active_path, environment)
    document = read_file(path)
    values, sources = {}, {}

    def selected(key):
        return sections is None or key.split(".", 1)[0] in sections

    for key, value in document.items():
        if not selected(key):
            continue
        try:
            values[key] = convert(FIELDS[key].kind, value)
        except (ValueError, TypeError, OverflowError):
            raise ConfigurationError(f"Invalid configuration field: {key}") from None
        sources[key] = "toml"
    for key, definition in FIELDS.items():
        if selected(key) and definition.environment in environment:
            try:
                values[key] = convert(
                    definition.kind, environment[definition.environment], environment=True
                )
            except (ValueError, TypeError, OverflowError):
                raise ConfigurationError(f"Invalid environment field: {key}") from None
            sources[key] = "environment"
    for key, value in {**active_overrides, **(overrides or {})}.items():
        if key not in FIELDS:
            raise ConfigurationError("Unknown command configuration field")
        if not selected(key):
            continue
        try:
            values[key] = convert(FIELDS[key].kind, value)
        except (ValueError, TypeError, OverflowError):
            raise ConfigurationError(f"Invalid command configuration field: {key}") from None
        sources[key] = "cli"
    state_environment = {key: environment[key] for key in ("XDG_STATE_HOME",) if key in environment}
    return Configuration(path, values, sources, state_environment)


@contextmanager
def configuration_scope(path=None, *, overrides=None):
    """Keep one absolute file selection for nested CLI entrypoints without env writes."""
    chosen = selected_path(path, os.environ)
    token = _SELECTION.set((chosen, overrides or {}))
    try:
        yield
    finally:
        _SELECTION.reset(token)

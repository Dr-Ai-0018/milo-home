"""Explicit newawz configuration for a private, persistent, direct host runtime."""

import hashlib
import json
import os
import re
import stat
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .config_loader import load_configuration

DISABLED = (
    "multi_agent",
    "apps",
    "plugins",
    "remote_plugin",
    "hooks",
    "goals",
    "daemon_auto_start",
    "browser_use",
    "computer_use",
    "in_app_browser",
)


def configured_profile():
    path = load_configuration(sections={"execution"}).get("execution.profile")
    if path is None:
        raise ValueError("Explicit provider profile selection is required")
    return path


def private_directory(path: Path, *, public_read=False):
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Runtime path must be absolute and non-linked")
    metadata = path.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (0o022 if public_read else 0o077)
    ):
        raise ValueError("Runtime directory must be private and owned by the caller")


def private_read(path: Path, *, limit=128 * 1024, private_parent=True) -> bytes:
    private_directory(path.parent, public_read=not private_parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > limit
        ):
            raise ValueError("Runtime file must be private, bounded and owned by the caller")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Runtime file too large")
        return data
    finally:
        os.close(descriptor)


def private_create(path: Path, data: bytes):
    private_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@dataclass(frozen=True)
class NewawzConfig:
    provider: str
    model: str
    env_key: str
    content: bytes = field(repr=False)

    @property
    def digest(self):
        return hashlib.sha256(self.content).hexdigest()

    @classmethod
    def load(cls, path: Path | None = None):
        if path is None:
            path = configured_profile()
        try:
            original = tomllib.loads(private_read(path, private_parent=False).decode())
            provider = original["model_provider"]
            model = original["model"]
            source = original["model_providers"][provider]
            return cls._validated(original, provider, model, source)
        except (KeyError, TypeError, UnicodeError, ValueError):
            raise ValueError("Invalid newawz configuration") from None

    @classmethod
    def _validated(cls, original, provider, model, source):
        if not isinstance(source, dict):
            raise ValueError("Invalid provider")
        if not isinstance(provider, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", provider):
            raise ValueError("Invalid provider")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Missing model")
        for name in ["name", "base_url", "env_key", "wire_api"]:
            if not isinstance(source.get(name), str) or not source[name]:
                raise ValueError("Incomplete provider")
        address = urlsplit(source["base_url"])
        if (
            address.scheme != "https"
            or not address.hostname
            or address.username
            or address.password
            or address.query
            or address.fragment
            or source["wire_api"] != "responses"
            or source.get("requires_openai_auth", False) is not False
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", source["env_key"])
            or source["env_key"] in {"PATH", "HOME", "CODEX_HOME", "LANG"}
        ):
            raise ValueError("Unsupported provider configuration")
        for name in ["auth", "env_http_headers", "query_params", "experimental_bearer_token"]:
            if source.get(name):
                raise ValueError("Unsupported provider authentication or routing")
        lines = ["model = " + json.dumps(model), "model_provider = " + json.dumps(provider)]
        for name in ["model_reasoning_effort", "service_tier"]:
            if name in original:
                if not isinstance(original[name], str):
                    raise ValueError("Invalid model setting")
                lines.append(name + " = " + json.dumps(original[name]))
        lines += [
            'approval_policy = "never"',
            'sandbox_mode = "danger-full-access"',
            'web_search = "disabled"',
            "[features]",
            "code_mode_host = true",
        ]
        lines.extend(name + " = false" for name in DISABLED)
        lines += ["[model_providers." + provider + "]"]
        for name in ["name", "base_url", "env_key", "wire_api"]:
            lines.append(name + " = " + json.dumps(source[name]))
        lines += [
            "requires_openai_auth = false",
            "request_max_retries = 0",
            "stream_max_retries = 0",
        ]
        headers = source.get("http_headers", {})
        if not isinstance(headers, dict) or any(
            not isinstance(value, str) or any(c in key + value for c in "\r\n\x00")
            for key, value in headers.items()
        ):
            raise ValueError("Invalid provider headers")
        if headers:
            lines.append("[model_providers." + provider + ".http_headers]")
            lines.extend(
                json.dumps(key) + " = " + json.dumps(headers[key]) for key in sorted(headers)
            )
        return cls(provider, model, source["env_key"], ("\n".join(lines) + "\n").encode())

    def with_model(self, model: str):
        if (
            not isinstance(model, str)
            or not 1 <= len(model) <= 200
            or any(character.isspace() or ord(character) < 32 for character in model)
        ):
            raise ValueError("A bounded model identifier without whitespace is required")
        original = tomllib.loads(self.content.decode())
        original["model"] = model
        return self._validated(
            original, self.provider, model, original["model_providers"][self.provider]
        )

    def materialize(self, home: Path):
        private_directory(home)
        path = home / "config.toml"
        try:
            private_create(path, self.content)
        except FileExistsError:
            if private_read(path) != self.content:
                raise ValueError("Stored runtime configuration changed") from None


@dataclass(frozen=True)
class CodexRuntime:
    cli: Path
    tool_host: Path

    def verify(self):
        installed_cli = self.tool_host.parent / "codex"
        for path in [self.cli, self.tool_host, installed_cli]:
            if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError("Explicit installed executables are required")
        if self.tool_host.name != "codex-code-mode-host":
            raise ValueError("Code Mode tool host is required")
        native_cli = installed_cli.resolve()
        if self.tool_host.resolve().parent != native_cli.parent:
            raise ValueError("Code Mode host and companion CLI must resolve to the same package")
        versions = set()
        for path in {self.cli.resolve(), native_cli}:
            result = subprocess.run(
                [str(path), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
            version = result.stdout.strip()
            if result.returncode or not re.fullmatch(r"codex-cli [^\s]{1,80}", version):
                raise ValueError("Cannot identify the installed Codex CLI")
            versions.add(version)
        if len(versions) != 1:
            raise ValueError("Codex CLI and tool-host package report different versions")
        # A matching package is only an installation check. App Server
        # compatibility is checked through the actual protocol before use.
        return versions.pop()

    def environment(self, home: Path, config: NewawzConfig, credential: str) -> bytes:
        private_directory(home)
        if not credential or len(credential) > 8192 or any(c in credential for c in "\r\n\x00"):
            raise ValueError("Missing or malformed provider credential")
        values = {
            "CODEX_HOME": str(home),
            "PATH": str(self.tool_host.parent) + ":/usr/bin:/bin",
            "LANG": "C.UTF-8",
            config.env_key: credential,
        }

        # systemd EnvironmentFile has no shell expansion, but uses its own quoting.
        def quote(value):
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

        return (
            "\n".join(name + "=" + quote(value) for name, value in values.items()) + "\n"
        ).encode()

    def command(self, *, workspace: Path | None = None):
        self.verify()
        command = [str(self.cli)]
        if workspace is not None:
            path = str(workspace)
            if (
                not workspace.is_absolute()
                or workspace.resolve() != workspace
                or not workspace.is_dir()
                or re.search(r"[\x00-\x1f\x7f\ud800-\udfff]", path)
            ):
                raise ValueError("Explicit canonical workspace is required")
            # Keep project-local configuration out of the dedicated runtime.
            # An explicit value also prevents native project trust writeback
            # from changing the on-disk profile and its binding fingerprint.
            quoted = json.dumps(path, ensure_ascii=False)
            command.extend(["-c", f'projects={{ {quoted}={{trust_level="untrusted"}} }}'])
        return [*command, "app-server", "--stdio"]


def same_installation_identity(recorded, expected):
    # Older records included the version. Compare identity without changing
    # the original evidence or fingerprints derived from its raw bytes.
    if not isinstance(recorded, dict):
        return False
    recorded = {key: value for key, value in recorded.items() if key != "version"}
    return json.dumps(recorded, sort_keys=True) == json.dumps(expected, sort_keys=True)

import json
import os
import re
import tomllib
from pathlib import Path

from .codex_config import configured_profile
from .path_policy import PathPolicy
from .reply_result import OUTPUT_SCHEMA, make_prompt

DISABLED_FEATURES = (
    "shell_snapshot",
    "shell_tool",
    "unified_exec",
    "multi_agent",
    "multi_agent_v2",
    "browser_use",
    "computer_use",
    "apps",
    "plugins",
    "remote_plugin",
    "image_generation",
    "view_image",
    "skill_search",
    "goals",
    "hooks",
    "sleep_tool",
    "code_mode_host",
    "daemon_auto_start",
    "in_app_browser",
)


def private_write(path: Path, text: str):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)


def prepare(
    base: Path, snapshot: dict, profile: Path | None, *, credential=None, configuration=None
) -> Path:
    PathPolicy.configured().require_external(base, directory=True)
    if configuration is None and profile is None:
        profile = configured_profile()
    base.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if base.parent.stat().st_mode & 0o077:
        raise ValueError("Run directory must be private")
    base.mkdir(mode=0o700, exist_ok=False)
    root = base / "rootfs"
    for name in [
        "usr",
        "etc/ssl/certs",
        "workspace",
        "runtime/codex",
        "root",
        "proc",
        "sys",
        "dev",
        "tmp",
    ]:
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in ["", "usr", "etc", "etc/ssl", "etc/ssl/certs", "workspace", "proc", "sys", "dev"]:
        (root / name).chmod(0o755)
    for name, target in [
        ("bin", "usr/bin"),
        ("sbin", "usr/sbin"),
        ("lib", "usr/lib"),
        ("lib64", "usr/lib64"),
    ]:
        (root / name).symlink_to(target)
    for name in ["resolv.conf", "hosts", "nsswitch.conf", "passwd", "group"]:
        path = root / "etc" / name
        path.write_bytes((Path("/etc") / name).read_bytes())
        path.chmod(0o644)
    original = tomllib.loads(
        configuration.decode() if configuration is not None else profile.read_text()
    )
    provider = original["model_provider"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", provider):
        raise ValueError("Unsupported provider identifier")
    source = original["model_providers"][provider]
    key_name = source["env_key"]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_name):
        raise ValueError("Unsupported credential variable")
    key = os.environ.get(key_name, "") if credential is None else credential
    if not key or any(character in key for character in "\r\n\x00"):
        raise ValueError("Missing provider credential")
    private_write(base / "credential.env", key_name + "=" + json.dumps(key) + "\n")
    config = []
    for name in ["model", "model_provider", "model_reasoning_effort", "service_tier"]:
        if name in original:
            config.append(name + " = " + json.dumps(original[name]))
    config += [
        'approval_policy = "never"',
        'sandbox_mode = "read-only"',
        'web_search = "disabled"',
        "[shell_environment_policy]",
        'inherit = "none"',
        'include_only = ["PATH", "LANG"]',
        "[agents]",
        "enabled = false",
        "[features]",
    ]
    config.extend(name + " = false" for name in DISABLED_FEATURES)
    config += ["[model_providers." + provider + "]"]
    for name in ["name", "base_url", "env_key", "wire_api"]:
        config.append(name + " = " + json.dumps(source[name]))
    config += ["request_max_retries = 0", "stream_max_retries = 0"]
    if source.get("http_headers"):
        config.append("[model_providers." + provider + ".http_headers]")
        for name, value in source["http_headers"].items():
            config.append(json.dumps(name) + " = " + json.dumps(value))
    private_write(root / "runtime/codex/config.toml", "")
    private_write(root / "runtime/codex/newawz.config.toml", "\n".join(config) + "\n")
    private_write(root / "workspace/schema.json", json.dumps(OUTPUT_SCHEMA))
    private_write(base / "prompt.txt", make_prompt(snapshot))
    # The process runs as nobody; only its own runtime is writable/readable.
    for name in ["runtime", "runtime/codex", "root", "tmp"]:
        path = root / name
        path.chmod(0o700)
        os.chown(path, 65534, 65534)
    for name in [
        "runtime/codex/config.toml",
        "runtime/codex/newawz.config.toml",
        "workspace/schema.json",
    ]:
        os.chown(root / name, 65534, 65534)
    return root


def unit_command(unit: str, root: Path, seconds: int, arguments: list[str]) -> list[str]:
    if not 1 <= seconds <= 3600:
        raise ValueError("Unit runtime must be 1–3600 seconds")
    base = root.parent
    command = ["systemd-run", "--quiet", "--wait", "--pipe", "--unit=" + unit]
    properties = [
        "CPUQuota=50%",
        "CPUQuotaPeriodSec=10ms",
        "AllowedCPUs=" + ",".join(map(str, sorted(os.sched_getaffinity(0)))),
        "MemoryMax=768M",
        "TasksMax=64",
        "User=65534",
        "Group=65534",
        "RootDirectory=" + str(root),
        "MountAPIVFS=yes",
        "BindReadOnlyPaths=/usr /etc/ssl/certs",
        "ProtectSystem=strict",
        "ReadWritePaths=+/runtime +/root +/tmp",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "ProtectProc=invisible",
        "RestrictSUIDSGID=yes",
        "KillMode=control-group",
        "TimeoutStopSec=3s",
        "RuntimeMaxSec=" + str(seconds) + "s",
        "WorkingDirectory=/workspace",
        "Environment=CODEX_HOME=/runtime/codex PATH=/usr/bin:/bin LANG=C.UTF-8",
    ]
    if (base / "credential.env").exists():
        properties.append("EnvironmentFile=" + str(base / "credential.env"))
    for property_ in properties:
        command += ["-p", property_]
    return command + arguments


def codex_arguments() -> list[str]:
    return [
        "/usr/bin/codex",
        "exec",
        "-p",
        "newawz",
        "-s",
        "read-only",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-rules",
        "--color",
        "never",
        "-C",
        "/workspace",
        "--output-schema",
        "/workspace/schema.json",
        "-o",
        "/runtime/answer.json",
        "-",
    ]

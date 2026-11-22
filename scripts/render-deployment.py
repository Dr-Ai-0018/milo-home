"""Render a single-site deployment for review; never install or start services."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from uuid import UUID

from milo_home.config_loader import load_configuration

TEMPLATES = Path(__file__).resolve().parents[1] / "deploy"
API_FILES = ("milo-home-api.service", "nginx-http.conf", "nginx-https.conf")
EXECUTION_FILES = (
    "milo-home-work.service",
    "milo-home-work.timer",
    "milo-home-reply.service",
    "milo-home-reply.timer",
    "milo-home-maintenance.service",
    "milo-home-maintenance.timer",
)


def absolute(value):
    path = Path(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or any(not (c.isalnum() or c in "/._-+ ") for c in str(path))
    ):
        raise ValueError(
            "Deployment paths require absolute names without expansion syntax"
        )
    return path


def quoted(value):
    # Inputs exclude specifiers, variables, quotes and backslashes. Spaces work
    # in executable names as well as arguments, without shell expansion.
    return '"' + str(value) + '"'


def cpu_selection(value, available):
    available = sorted(available)
    if value is None:
        return available[: max(1, len(available) // 2)]
    if not re.fullmatch(r"[0-9]+(?:,[0-9]+)*", value):
        raise ValueError("CPU selection requires comma-separated CPU numbers")
    chosen = sorted(set(map(int, value.split(","))))
    if not set(chosen) <= set(available):
        raise ValueError("CPU selection must be inside the current allowed set")
    return chosen


def execution_values(args, required, writable, protected):
    if args.owner is None or args.session is None:
        raise ValueError(
            "Execution templates require explicit registered owner and session UUIDs"
        )
    for field in (
        "run_directory",
        "reply_directory",
        "workspace",
        "profile",
        "credential_file",
    ):
        target = required("execution." + field)
        if any(target.is_relative_to(root) for root in writable + protected):
            raise ValueError(
                "Private execution paths must be outside API and protected roots"
            )
    summary = required("maintenance.file")
    if str(required("maintenance.owner")) != str(args.owner):
        raise ValueError("Maintenance owner must match the explicit execution owner")
    if any(summary.is_relative_to(root) for root in writable + protected):
        raise ValueError(
            "Maintenance summary must stay outside API and protected roots"
        )
    return {
        "OWNER": str(args.owner),
        "SESSION": str(args.session),
        "CREDENTIAL": str(required("execution.credential_file")),
        "REPLY_DIRECTORY": quoted(required("execution.reply_directory")),
        "NATIVE_HOME": quoted(required("execution.run_directory")),
    }


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--config", type=absolute, required=True)
    command.add_argument("--python", type=absolute, required=True)
    command.add_argument("--working-directory", type=absolute, required=True)
    command.add_argument("--domain", required=True)
    command.add_argument("--acme-root", type=absolute, required=True)
    command.add_argument("--certificate", type=absolute, required=True)
    command.add_argument("--certificate-key", type=absolute, required=True)
    command.add_argument("--port", type=int, default=7474)
    command.add_argument(
        "--cpus", help="Allowed current CPU subset; default is half, minimum one"
    )
    command.add_argument(
        "--execution", action="store_true", help="Also render disabled timer files"
    )
    command.add_argument("--owner", type=UUID)
    command.add_argument("--session", type=UUID)
    command.add_argument(
        "--out",
        type=absolute,
        help="New output directory; omitted means stdout preview",
    )
    return command


def replacements(args, configuration, available):
    domain = args.domain.lower()
    if len(domain) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in domain.split(".")
    ):
        raise ValueError("A single DNS domain is required")
    if not 1 <= args.port <= 65535:
        raise ValueError("API port must be between 1 and 65535")
    if configuration.get("app.origins") != ("https://" + domain,):
        raise ValueError(
            "Deployment requires exactly its HTTPS origin in the selected file"
        )
    if not configuration.get("app.secure_cookie"):
        raise ValueError("HTTPS deployment requires secure cookies")

    def required(key):
        if key not in configuration.sources:
            raise ValueError("Missing deployment configuration field: " + key)
        value = configuration.get(key)
        return absolute(value) if isinstance(value, Path) else value

    account = required("execution.database_user")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", account) or account == "root":
        raise ValueError("API requires a dedicated non-root service account")
    code = required("paths.code_root")
    static = required("paths.static_root")
    database = required("app.database")
    writable = [database.parent]
    for section in ("work_attachments", "public_assets"):
        root = configuration.get(section + ".root")
        budget = configuration.get(section + ".max_bytes")
        if (root is None) != (budget is None):
            raise ValueError("Incomplete deployment storage configuration")
        if root is not None:
            writable.append(absolute(root))
    protected = [code, static, *map(absolute, configuration.get("paths.protected_roots"))]
    for target in writable:
        if any(target.is_relative_to(p) or p.is_relative_to(target) for p in protected):
            raise ValueError(
                "Writable data must be separate from protected deployment roots"
            )
    if any(args.config.is_relative_to(root) for root in writable):
        raise ValueError(
            "Deployment configuration must stay outside API-writable roots"
        )
    if not args.python.is_relative_to(
        code
    ) or not args.working_directory.is_relative_to(code):
        raise ValueError(
            "Interpreter and working directory must belong to the configured code root"
        )
    cpus = cpu_selection(args.cpus, available)
    result = {
        "PYTHON": quoted(args.python),
        "CONFIG_ARG": quoted(args.config),
        "CONFIG_ENV": quoted("MILO_CONFIG=" + str(args.config)),
        # WorkingDirectory is a single path directive, unlike ExecStart's
        # token list; systemd treats surrounding quotes as part of this path.
        "WORKING_DIRECTORY": str(args.working_directory),
        "ACCOUNT": account,
        "DOMAIN": domain,
        "PORT": str(args.port),
        "STATIC_ROOT": quoted(static),
        "ACME_ROOT": quoted(args.acme_root),
        "CERTIFICATE": quoted(args.certificate),
        "CERTIFICATE_KEY": quoted(args.certificate_key),
        "WRITABLE": " ".join(quoted(p) for p in dict.fromkeys(writable)),
        "CPUS": ",".join(map(str, cpus)),
        "AFFINITY": " ".join(map(str, cpus)),
        "BODY_LIMIT": str(configuration.get("app.body_limit")),
    }
    if args.execution:
        result.update(execution_values(args, required, writable, protected))
    return result


def render(args, *, available=None):
    # Deployment previews represent the explicitly selected file, not whatever
    # business environment happens to be inherited by the operator's shell.
    configuration = load_configuration(args.config, environment={})
    values = replacements(
        args, configuration, os.sched_getaffinity(0) if available is None else available
    )
    files = API_FILES + (EXECUTION_FILES if args.execution else ())
    rendered = {}
    for name in files:
        template = (TEMPLATES / name).read_text()

        def substitute(match):
            return values[match[1]]

        rendered[name] = re.sub(r"@@([A-Z_]+)@@", substitute, template)
    rendered["manifest.json"] = (
        json.dumps(
            {
                "version": 1,
                "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
                "cpus": values["CPUS"],
                "execution_templates": args.execution,
                "installed": False,
                "files": {
                    key: hashlib.sha256(value.encode()).hexdigest()
                    for key, value in rendered.items()
                },
            },
            indent=2,
        )
        + "\n"
    )
    return rendered


def main():
    command = parser()
    args = command.parse_args()
    try:
        files = render(args)
        if args.out is None:
            print(json.dumps(files, ensure_ascii=False, indent=2))
        else:
            args.out.mkdir(mode=0o700)
            for name, text in files.items():
                path = args.out / name
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(text)
            print("Deployment files rendered; nothing installed or enabled.")
    except (ValueError, OSError, KeyError):
        command.exit(
            1,
            "Deployment preview failed; check fields, paths and new output directory.\n",
        )


if __name__ == "__main__":
    main()

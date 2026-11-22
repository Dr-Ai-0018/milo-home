"""Apply deployment defaults only to command options that actually use them."""

from .config import Settings
from .config_loader import load_configuration

RUN_COMMANDS = {
    "work",
    "reply-service",
    "automatic-service",
    "scheduler",
    "wake",
    "stop",
    "run-status",
    "recover-runs",
}
ACCOUNT_COMMANDS = RUN_COMMANDS | {"content", "reception", "revisit"}


def command_overrides(args):
    """Parser defaults are None; only real global CLI inputs override lower sources."""
    selected = {}
    for name, key in (
        ("database", "app.database"),
        ("origin", "app.origins"),
        ("database_user", "execution.database_user"),
        ("run_directory", "execution.run_directory"),
    ):
        value = getattr(args, name, None)
        if value is not None:
            selected[key] = value
    return selected


def apply_command_defaults(args):
    command = args.command
    capture = command == "backup-paired" and args.paired_action == "capture"
    maintenance = command == "maintenance" and args.maintenance_action not in {
        "rehearse",
        "restore-entries",
        "show",
    }
    if command not in ACCOUNT_COMMANDS and not capture and not maintenance:
        return
    configuration = load_configuration()
    if (command in RUN_COMMANDS or capture or maintenance) and (
        "app.database" not in configuration.sources
    ):
        raise ValueError("Private execution and observation require an explicit database")

    def fill(name, key):
        if getattr(args, name, None) is None:
            setattr(args, name, configuration.get(key))

    fill("database_user", "execution.database_user")
    if command in RUN_COMMANDS or capture or maintenance:
        fill("run_directory", "execution.run_directory")
    if capture or maintenance:
        if hasattr(args, "workspace"):
            fill("workspace", "execution.workspace")
            if args.workspace is None:
                raise ValueError("An explicit workspace is required")
        if hasattr(args, "reply_directory"):
            fill("reply_directory", "execution.reply_directory")
    if command in {"work", "reply-service"}:
        if hasattr(args, "credential_file"):
            fill("credential_file", "execution.credential_file")
        if hasattr(args, "profile"):
            fill("profile", "execution.profile")
        for name in ("cli", "tool_host"):
            if hasattr(args, name):
                fill(name, f"execution.{name}")
                if getattr(args, name) is None:
                    raise ValueError("Main initialization requires explicit installed executables")
    if maintenance:
        for name in ("backup_directory", "development_directory", "success_file", "daily_unit"):
            if hasattr(args, name):
                fill(name, f"maintenance.{name}")


def configuration_report():
    configuration = load_configuration()
    Settings.from_configuration(configuration)
    return {
        "valid": True,
        "configured_file": configuration.path is not None,
        "sources": configuration.source_report(),
        "runtime_files_checked": False,
        "automatic_execution": False,
    }

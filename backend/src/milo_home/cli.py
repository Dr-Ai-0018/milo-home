"""Local CLI entry and sanitized operational error boundary."""

import sqlite3
import subprocess

from .cli_arguments import build_parser
from .cli_commands import execute
from .codex_rpc import DeliveryUnknown, ProtocolError, RequestRejected
from .config_cli import command_overrides
from .config_loader import configuration_scope
from .errors import ApiError
from .execution_lock import ExecutionIdentityError


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        with configuration_scope(args.config, overrides=command_overrides(args)):
            execute(args)
    except (
        ValueError,
        OSError,
        sqlite3.Error,
        ApiError,
        ProtocolError,
        DeliveryUnknown,
        RequestRejected,
        ExecutionIdentityError,
        subprocess.SubprocessError,
    ) as error:
        # Do not echo submitted values or namespaces containing secrets.
        message = (
            error.message if isinstance(error, ApiError) else "Configuration or file check failed."
        )
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()

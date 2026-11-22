"""Bound the timer's one maintenance process without changing any other service."""

import argparse
import os
import signal
import subprocess
import sys
import time

from .config_loader import load_configuration
from .run_transport import cpu_sample


def busy(before, after):
    total = after[0] - before[0]
    return 100 * (1 - (after[1] - before[1]) / total) if total else 0


def stop(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)


def guarded(command, *, seconds=40):
    before = cpu_sample()
    time.sleep(0.5)
    current = cpu_sample()
    if busy(before, current) >= 70:
        print("Maintenance deferred: host is busy.", flush=True)
        return 0
    process = subprocess.Popen(command, start_new_session=True)
    deadline = time.monotonic() + seconds
    try:
        while process.poll() is None:
            time.sleep(0.5)
            sample = cpu_sample()
            if busy(current, sample) >= 80 or time.monotonic() >= deadline:
                print("Maintenance stopped at host or time limit.", flush=True)
                return 80
            current = sample
        return process.returncode
    finally:
        stop(process)


def build_command(configuration):
    directory = configuration.get("execution.run_directory")
    workspace = configuration.get("execution.workspace")
    reply = configuration.get("execution.reply_directory")
    if directory is None or workspace is None or reply is None:
        raise ValueError("Maintenance paths must be explicitly configured")
    command = [sys.executable, "-m", "milo_home.cli"]
    if configuration.path is not None:
        command.extend(["--config", str(configuration.path)])
    account = configuration.get("execution.database_user")
    if account is not None:
        command.extend(["--database-user", account])
    command.extend(
        [
            "--run-directory",
            str(directory),
            "maintenance",
            "run",
            "--workspace",
            str(workspace),
            "--reply-directory",
            str(reply),
            "--apply",
        ]
    )
    # File selection and explicit paths survive any child cwd; other overrides
    # remain in the inherited environment and use the same resolver.
    return command


def main():
    parser = argparse.ArgumentParser(description="Run one bounded maintenance observation")
    parser.add_argument("--config")
    args = parser.parse_args()
    try:
        command = build_command(load_configuration(args.config))
    except (ValueError, OSError):
        parser.exit(1, "Maintenance configuration could not be validated.\n")

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        raise SystemExit(guarded(command))
    except KeyboardInterrupt:
        raise SystemExit(143) from None


if __name__ == "__main__":
    main()

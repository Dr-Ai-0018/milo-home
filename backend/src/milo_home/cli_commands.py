"""Existing local command operations; public parsing and errors live separately."""

import json
import os
import secrets
from pathlib import Path

import uvicorn

from .app import create_app
from .auth import AuthService
from .config import Settings
from .config_cli import apply_command_defaults, configuration_report
from .database import Database
from .path_policy import PathPolicy
from .schedule_cli import handle as handle_schedule
from .schemas import LoginInput


def private_output(path: Path) -> Path:
    PathPolicy.configured().require_external(path)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("Private output directory must have permissions 0700")
    return path


def handle_independent(args):
    if args.command == "config-check":
        print(json.dumps(configuration_report(), ensure_ascii=False))
        return True
    if args.command == "export-openapi":
        # Export is offline and must not consult live deployment or runtime credentials.
        settings = Settings(
            database=Path("/schema-only/home.sqlite3"), origins=("https://localhost",)
        )
        with args.out.open("w", encoding="utf-8") as stream:
            json.dump(create_app(settings).openapi(), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print("Public OpenAPI schema exported.")
        return True
    if args.command == "maintenance":
        from .maintenance_cli import handle

        handle(args)
        return True
    if args.command == "backup-paired":
        from .paired_backup_cli import handle

        handle(args)
        return True
    if args.command == "backup-native-session":
        from .native_session_backup_cli import handle

        handle(args)
        return True
    if args.command == "backup-workspace":
        from .workspace_backup_cli import handle

        handle(args)
        return True
    if args.command == "dev-fixture":
        from .dev_fixture import create_fixture

        directory = create_fixture(args.parent, args.scenario)
        print(f"Synthetic preview created: {directory}")
        print("Login details are in login.json; no service or model call was started.")
        return True
    if args.command == "backup-rehearse":
        from .backup_rehearsal import rehearse_backup

        report = rehearse_backup(args.file, args.out)
        print(json.dumps(report, ensure_ascii=False))
        return True
    return False


def database_context(settings, args):
    database = Database(settings.database, paths=settings.paths)
    runtime = None
    if args.command in {"reception", "revisit", "content"}:
        if args.run_directory:
            raise ValueError("Interaction control does not use a private execution directory")
        if args.database_user:
            from .database_identity import ServiceDatabase

            database = ServiceDatabase(settings.database, args.database_user)
        return database, None
    if args.command in {"work", "reply-service", "automatic-service"}:
        from .database_identity import ServiceDatabase, private_run_directory

        if args.run_directory is None:
            raise ValueError("Execution preparation requires an existing private run directory")
        runtime = private_run_directory(args.run_directory)
        if args.database_user:
            database = ServiceDatabase(settings.database, args.database_user)
        return database, runtime
    if args.database_user or args.run_directory:
        from .database_identity import ServiceDatabase, private_run_directory

        if (
            args.command not in {"wake", "stop", "run-status", "recover-runs", "scheduler"}
            or not args.database_user
            or not args.run_directory
        ):
            raise ValueError("Account and runtime options are required together for run commands")
        runtime = private_run_directory(args.run_directory)
        database = ServiceDatabase(settings.database, args.database_user)
    return database, runtime


def handle_before_initialization(database, runtime, settings, args):
    if args.command == "content":
        if args.content_action == "work":
            from .public_works_cli import handle as handle_work

            handle_work(database, settings, args)
            return True
        from .public_notes_cli import handle

        handle(database, args)
        return True
    if args.command == "revisit":
        from .revisit_cli import handle

        handle(database, args)
        return True
    if args.command == "reception":
        from .reception_cli import handle

        handle(database, args)
        return True
    if args.command == "automatic-service":
        from .service_control_cli import handle

        handle(database, args)
        return True
    if args.command == "reply-service":
        from .reply_service_cli import handle

        handle(database, args, runtime)
        return True
    if args.command == "work":
        from .work_cli import handle

        handle(database, runtime, args)
        return True
    if args.command == "scheduler":
        from .scheduler_cli import handle

        handle(database, args, runtime)
        return True
    if args.command == "doctor":
        from .database_inspection import inspect_database

        report = inspect_database(database.path)
        print(json.dumps(report, ensure_ascii=False))
        if not report["database_ok"]:
            raise SystemExit(1)
        return True
    if args.command == "usage":
        from .usage import inspect_usage

        print(json.dumps(inspect_usage(database.path, args.day), ensure_ascii=False))
        return True
    if args.command == "serve":
        uvicorn.run(
            create_app(settings),
            host="127.0.0.1",
            port=args.port,
            proxy_headers=False,
            access_log=False,
        )
        return True
    if args.command == "backup":
        database.backup(private_output(args.out))
        print("Private database backup verified.")
        return True
    return False


def handle_run(database, runtime, args):
    from .runs import RunStore
    from .schemas import RunView
    from .worker import Worker, install_stop_handler

    store = RunStore(database)
    if args.command == "stop":
        store.get(str(args.run))
        store.stop(str(args.run))
        print("Cancellation requested; check run-status for confirmed completion.")
    elif args.command == "recover-runs":
        Worker(database, runtime=runtime).recover()
        print("Previous run termination verified; no automatic retry started.")
    else:
        if args.command == "wake":
            install_stop_handler()
            run = Worker(database, runtime=runtime).wake(str(args.thread), args.timeout)
        else:
            run = store.get(str(args.run))
        print(RunView.model_validate(run).model_dump_json())
        if args.command == "wake" and run["status"] != "succeeded":
            raise SystemExit(1)
    return


def create_invitation(database, settings, args):
    LoginInput(handle=args.handle, password="validation-only-password")
    output = private_output(args.out)
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        token = secrets.token_urlsafe(32)
        with os.fdopen(descriptor, "w") as stream:
            descriptor = -1
            json.dump(
                {
                    "handle": args.handle,
                    "token": token,
                },
                stream,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        AuthService(database, settings).create_invitation(args.handle, token=token)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        output.unlink(missing_ok=True)
        raise
    print("Invitation saved to the requested private file; expires in 48 hours.")


def handle_initialized(database, runtime, settings, args):
    database.initialize()
    if args.command == "events":
        from .wakeup_events import EventPlanner

        planner = EventPlanner(database)
        if args.event_action == "reconcile":
            from .dispatch import DispatchStore

            result = {
                "automatic_wakeup": False,
                **DispatchStore(database).reconcile(),
            }
        else:
            result = planner.plan() if args.event_action == "plan" else planner.status()
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.command == "schedule":
        handle_schedule(database, args)
        return
    if args.command in {"wake", "stop", "run-status", "recover-runs"}:
        handle_run(database, runtime, args)
        return
    if args.command == "init-db":
        print("Database schema verified.")
        return
    create_invitation(database, settings, args)


def execute(args):
    apply_command_defaults(args)
    if handle_independent(args):
        return
    settings = Settings.from_env()
    database, runtime = database_context(settings, args)
    if handle_before_initialization(database, runtime, settings, args):
        return
    handle_initialized(database, runtime, settings, args)

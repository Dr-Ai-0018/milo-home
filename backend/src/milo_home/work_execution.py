"""One explicitly requested local execution, using only the registered main."""

import json
import time
from dataclasses import asdict
from pathlib import Path

from .automatic_services import AutomaticServices
from .codex_config import NewawzConfig
from .codex_journal import PrivateJournal
from .config import WorkAttachmentSettings
from .scheduler_cli import StopRequest, stop_signals
from .work_attachment_store import AttachmentStore
from .work_credentials import load_credential
from .work_preflight import check_receiver
from .work_process import WorkLauncher
from .work_receiver import ReceiveLimit, WorkReceiver, receive_once
from .work_runtime import WorkRuntime
from .work_service_control import observe_work_entry, registered_work_errors

DEFAULT_WORK_TIMEOUT = 3600


def add_run_command(actions):
    from uuid import UUID

    run = actions.add_parser("run", help="显式执行一件已保存任务，直接操作本机，可能产生费用")
    run.add_argument("--owner", type=UUID, required=True)
    run.add_argument("--task", type=UUID, required=True)
    run.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_WORK_TIMEOUT,
        help="1–3600秒（默认3600），准备与执行共用期限",
    )
    run.add_argument("--execute", action="store_true", required=True)
    run.add_argument("--credential-file", type=Path)


def add_listen_command(actions):
    from uuid import UUID

    listen = actions.add_parser("listen", help="有限等候并执行主会话最早排队任务，至多一件")
    listen.add_argument("--owner", type=UUID, required=True)
    listen.add_argument("--session", type=UUID, required=True)
    listen.add_argument("--timeout", type=int, required=True, help="1–3600秒，等候/准备/执行共用")
    listen.add_argument("--interval", type=int, default=2, help="本机收件检查间隔，1–30秒")
    listen.add_argument("--execute", action="store_true", required=True)
    listen.add_argument("--credential-file", type=Path)

    once = actions.add_parser("take-next", help="查一次队列，按作息至多执行一件")
    once.add_argument("--owner", type=UUID, required=True)
    once.add_argument("--session", type=UUID, required=True)
    once.add_argument("--timeout", type=int, default=DEFAULT_WORK_TIMEOUT)
    once.add_argument("--execute", action="store_true", required=True)
    once.add_argument("--credential-file", type=Path)


def add_check_command(actions):
    from uuid import UUID

    check = actions.add_parser("check", help="纯读核对有限收件条件，不启动或授权执行")
    check.add_argument("--owner", type=UUID, required=True)
    check.add_argument("--session", type=UUID, required=True)
    check.add_argument(
        "--timeout", type=int, default=DEFAULT_WORK_TIMEOUT, help="1–3600秒的预算投影（默认3600）"
    )
    check.add_argument("--credential-file", type=Path)


def handle(database, directory, args):
    checking = args.work_action == "check"
    if (not checking and not args.execute) or not 1 <= args.timeout <= DEFAULT_WORK_TIMEOUT:
        raise ValueError("Explicit execution and a 1–3600 second timeout are required")
    stop = StopRequest()
    with stop_signals(stop):
        listening = args.work_action == "listen"
        limit = ReceiveLimit(args.timeout, time.time) if listening else None
        controlled = None
        with registered_work_errors(
            database, str(args.owner), configuration=True, active=args.work_action == "take-next"
        ):
            if args.work_action == "take-next":
                controlled = observe_work_entry(database, str(args.owner), str(args.session))
                if controlled is False:
                    print(json.dumps({"outcome": "service_disabled_or_faulted", "turns": 0}))
                    return
                if controlled and args.credential_file is None:
                    raise ValueError("Registered work requires an explicit credential file")
            journal = PrivateJournal(directory, initialize=False, read_only=checking)
            config = NewawzConfig.load()
            installed = journal.installation()
            credential = load_credential(config, args.credential_file)
            attachments = WorkAttachmentSettings.from_env() if not checking else None
            store = (
                AttachmentStore(
                    attachments.root,
                    max_bytes=attachments.max_bytes,
                    owner=getattr(database, "uid", None),
                )
                if attachments
                else None
            )
        runtime = WorkRuntime(database, journal)
        if checking:
            report = check_receiver(
                runtime,
                config,
                installed,
                str(args.owner),
                str(args.session),
                args.timeout,
                credential,
                stop,
            )
            print(json.dumps(report))
            return
        launcher = WorkLauncher(
            runtime, config, installed, stop_requested=stop.is_set, attachment_store=store
        )
        if args.work_action == "take-next":
            with registered_work_errors(database, str(args.owner)):
                take_next(launcher, stop, args, credential, controlled)
            return
        if listening:
            receiver = WorkReceiver(launcher, stop, limit, interval=args.interval)
            report = receiver.run(str(args.owner), str(args.session), credential=credential)
            print(json.dumps(asdict(report)))
            if report.outcome != "completed":
                raise SystemExit(1)
            return
        window_id, result = launcher.run_task(
            str(args.owner), str(args.task), args.timeout, credential=credential
        )
    print(json.dumps({"window_id": window_id, "outcome": result.outcome, "turns": result.turns}))
    if result.outcome != "completed":
        raise SystemExit(1)


def take_next(launcher, stop, args, credential, controlled):
    runtime = launcher.runtime
    if controlled is not None:
        next_observation = [time.monotonic() + 15]

        def managed_stop():
            if time.monotonic() >= next_observation[0]:
                observe_work_entry(
                    runtime.database, str(args.owner), str(args.session), runtime.clock
                )
                next_observation[0] = time.monotonic() + 15
            return stop.is_set()

        launcher.stop_requested = managed_stop
    report = receive_once(
        launcher, stop, str(args.owner), str(args.session), args.timeout, credential=credential
    )
    # Omit task and window identifiers from the recurring service journal.
    print(json.dumps({"outcome": report.outcome, "turns": report.turns}))
    if report.task_id is not None and report.outcome != "completed":
        if controlled is not None:
            AutomaticServices(runtime.database, runtime.clock).fault(
                "work", str(args.owner), "execution_failed"
            )
        raise SystemExit(1)

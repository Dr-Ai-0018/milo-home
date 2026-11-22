"""Independent preparation, finite execution, and explicit recovery."""

import json
from pathlib import Path
from uuid import UUID

from .reply_readiness import check_reply_service


def add_commands(commands):
    service = commands.add_parser("reply-service", help="普通回信准备、单批执行与明确恢复")
    actions = service.add_subparsers(dest="reply_service_action", required=True)
    check = actions.add_parser("check", help="只读核对本人范围、凭据和运行条件，不调用模型")
    check.add_argument("--owner", type=UUID, required=True)
    check.add_argument("--credential-file", type=Path)
    check.add_argument("--profile", type=Path, help="本次显式覆盖部署配置的profile路径")
    check.add_argument("--model", help="只覆盖本入口模型；渠道及凭据仍取指定 newawz 配置")
    check.add_argument("--native-home", type=Path, help="只读核对已有原生主对话与执行策略")
    check.add_argument("--timeout", type=int, default=3600)
    once = actions.add_parser("take-next", help="检查一次队列，至多回一批，可能调用模型")
    once.add_argument("--owner", type=UUID, required=True)
    once.add_argument("--credential-file", type=Path)
    once.add_argument("--profile", type=Path, help="本次显式覆盖部署配置的profile路径")
    once.add_argument("--model", help="只覆盖本入口模型；省略时沿用 newawz 中的模型")
    once.add_argument(
        "--native-home", type=Path, help="明确接续已有原生主对话，须先登记auto review策略"
    )
    once.add_argument("--timeout", type=int, default=3600)
    once.add_argument("--execute", action="store_true", required=True)
    for name, help_ in [
        ("stop", "只请求停止本账号原回信，不关闭交办"),
        ("replay", "确认执行组停止后，只补原有效结果，不重调模型"),
        ("recover", "明确停止并核对原未知执行，不重发旧输入"),
    ]:
        action = actions.add_parser(name, help=help_)
        action.add_argument("--owner", type=UUID, required=True)
        action.add_argument("--run", type=UUID, required=True)
        if name == "recover":
            action.add_argument("--confirm-stop", action="store_true", required=True)


def handle(database, args, runtime=None):
    if args.reply_service_action in {"check", "take-next"} and args.credential_file is None:
        raise ValueError("Registered reply service requires an explicit credential file")
    if args.reply_service_action != "check":
        handle_execution(database, runtime, args)
        return
    report = check_reply_service(
        database,
        str(args.owner),
        args.profile,
        args.credential_file,
        seconds=args.timeout,
        model=args.model,
        native_home=args.native_home,
    )
    print(json.dumps(report))
    if not report["ready"]:
        raise SystemExit(1)


def handle_execution(database, runtime, args):
    from .reply_execution import run_once
    from .reply_recovery import recover, replay, request_stop
    from .scheduler_cli import StopRequest, stop_signals

    owner = str(args.owner)
    if args.reply_service_action == "take-next":
        stop = StopRequest()
        with stop_signals(stop):
            report = run_once(
                database,
                runtime,
                owner,
                args.profile,
                args.credential_file,
                args.timeout,
                stop,
                model=args.model,
                native_home=args.native_home,
            )
    elif args.reply_service_action == "stop":
        report = request_stop(database, owner, str(args.run))
    elif args.reply_service_action == "replay":
        report = replay(database, runtime, owner, str(args.run))
    else:
        report = recover(database, runtime, owner, str(args.run))
    print(json.dumps(report))
    if report["outcome"] in {"failed", "timed_out", "needs_reconciliation", "publication_blocked"}:
        raise SystemExit(1)

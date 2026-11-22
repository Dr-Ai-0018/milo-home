"""Local registration and recovery controls; installing a timer is a separate action."""

import argparse
import json
from uuid import UUID

from .automatic_services import AutomaticServices, require_entry


def add_commands(commands):
    service = commands.add_parser("automatic-service", help="登记、停用或恢复独立入口，不启动模型")
    actions = service.add_subparsers(dest="service_action", required=True)
    for name, help_ in [
        ("register", "明确登记账号、timer及交办主会话，不安装或启动timer"),
        ("status", "只读查看指定入口的脱敏登记与故障"),
        ("disable", "只关闭此入口的新领取，不取消在途或另一入口"),
        ("resume", "核对无遗留占用后明确清故障并允许新领取"),
    ]:
        action = actions.add_parser(name, help=help_)
        action.add_argument("--kind", choices=["reply", "work"], required=True)
        action.add_argument("--owner", type=UUID, required=True)
        if name == "register":
            action.add_argument("--timer-unit", required=True)
            action.add_argument("--session", type=UUID)
            action.add_argument("--enable", action="store_true")
            action.add_argument(
                "--revisits",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="明确普通原生入口支持到期回访；不启动服务",
            )
            action.add_argument(
                "--work-decisions",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="明确交办原生入口支持决定、原单澄清和暂缓回访；不启动服务",
            )


def handle(database, args):
    owner = str(args.owner)
    store = AutomaticServices(database)
    if args.service_action == "register":
        store.configure(
            args.kind,
            owner,
            args.timer_unit,
            session=str(args.session) if args.session else None,
            enabled=args.enable,
            revisits=args.revisits,
            work_decisions=args.work_decisions,
        )
    elif args.service_action == "disable":
        store.disable(args.kind, owner)
    elif args.service_action == "resume":
        store.resume(args.kind, owner)
    with database.snapshot() as connection:
        row = require_entry(connection, args.kind, owner)
        report = {
            "kind": row["kind"],
            "enabled": bool(row["enabled"]),
            "fault": row["fault_code"],
            "revision": row["revision"],
            "checked_at": row["checked_at"],
            "automatic_execution_requested": False,
        }
        capability = connection.execute(
            "SELECT enabled FROM reply_revisit_capabilities WHERE owner_id=?", (owner,)
        ).fetchone()
        if args.kind == "reply" and capability is not None:
            report["revisits_enabled"] = bool(capability[0])
        work = connection.execute(
            "SELECT enabled FROM work_decision_capabilities WHERE owner_id=?", (owner,)
        ).fetchone()
        if args.kind == "work" and work is not None:
            report["work_decisions_enabled"] = bool(work[0])
        print(json.dumps(report))

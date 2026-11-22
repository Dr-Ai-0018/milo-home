"""Controlled local revision of reception, without changing schedules or starting work."""

import json
import time
from datetime import datetime
from uuid import UUID

from .database import CURRENT_SCHEMA_VERSION
from .interaction_timing import ReceptionRequest
from .reception import KINDS, observe, save, settle_due


def add_commands(commands):
    parser = commands.add_parser("reception", help="本机查看或修订本人接收安排，不启动执行")
    actions = parser.add_subparsers(dest="reception_action", required=True)
    show = actions.add_parser("show", help="读取当前两个接收范围")
    show.add_argument("--owner", type=UUID, required=True)
    update = actions.add_parser("set", help="保存明确接收选择，不改管理员作息")
    update.add_argument("--owner", type=UUID, required=True)
    update.add_argument("--scope", choices=[*KINDS, "all"], required=True)
    update.add_argument(
        "--mode", choices=["available", "focusing", "resting", "later"], required=True
    )
    update.add_argument("--resume-on", choices=["at_time", "new_input", "explicit"])
    update.add_argument("--until", help="at_time恢复时刻，带时区的ISO时间")


def handle(database, args, *, clock=time.time):
    owner, now = str(args.owner), int(clock())
    request = None
    if args.reception_action == "set":
        until = None
        if args.until is not None:
            value = datetime.fromisoformat(args.until)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Reception resume time must include a timezone")
            until = int(value.timestamp())
        request = ReceptionRequest(
            scope=args.scope, mode=args.mode, resume_on=args.resume_on, until=until
        )
    with database.connect(write=request is not None) as connection:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        if connection.execute("PRAGMA user_version").fetchone()[0] != CURRENT_SCHEMA_VERSION:
            raise ValueError("Reception control requires the current schema; it does not migrate")
        if not connection.execute("SELECT 1 FROM users WHERE id=?", (owner,)).fetchone():
            raise ValueError("Reception owner does not exist")
        if request is not None:
            settle_due(connection, now, owner=owner)
            save(connection, owner, request, now)
        result = {kind: observe(connection, owner, kind, now, now + 45) for kind in KINDS}
    print(
        json.dumps(
            {
                kind: value.model_dump(mode="json") if value else None
                for kind, value in result.items()
            },
            ensure_ascii=False,
        )
    )

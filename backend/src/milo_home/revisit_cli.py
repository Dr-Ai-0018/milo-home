"""Local, explicit revisit changes using the already authenticated owner and expected revision."""

import json
from datetime import datetime
from uuid import UUID

from .database import CURRENT_SCHEMA_VERSION
from .interaction_timing import RevisitView
from .revisit_control import RevisitControl


def add_commands(commands):
    parser = commands.add_parser("revisit", help="本机查看、改期或取消已保存回访，不启动执行")
    actions = parser.add_subparsers(dest="revisit_action", required=True)
    for name in ("show", "reschedule", "cancel"):
        action = actions.add_parser(name)
        action.add_argument("--owner", type=UUID, required=True)
        if name != "show":
            action.add_argument("--id", type=UUID, required=True)
            action.add_argument("--revision", type=int, required=True)
        if name == "reschedule":
            action.add_argument("--until", required=True, help="带时区的ISO回访时间")
        if name == "cancel":
            action.add_argument(
                "--no-reply", action="store_true", help="明确结束关联的暂缓，不再接话"
            )


def view(row):
    fields = ("id", "revision", "state", "due_at", "updated_at", "blocked_reason")
    return RevisitView.model_validate({key: row[key] for key in fields}).model_dump(mode="json")


def handle(database, args):
    owner = str(args.owner)
    with database.snapshot() as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0] != CURRENT_SCHEMA_VERSION:
            raise ValueError("Revisit control requires the current schema; it does not migrate")
    if args.revisit_action == "show":
        with database.snapshot() as connection:
            rows = connection.execute(
                "SELECT * FROM revisit_intents WHERE owner_id=? ORDER BY due_at,id", (owner,)
            ).fetchall()
        result = [view(row) for row in rows]
    else:
        due_at = None
        if args.revisit_action == "reschedule":
            value = datetime.fromisoformat(args.until)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Revisit time must include a timezone")
            due_at = int(value.timestamp())
        row = RevisitControl(database).change(
            owner,
            str(args.id),
            args.revision,
            action=args.revisit_action,
            due_at=due_at,
            no_reply=getattr(args, "no_reply", False),
        )
        result = view(row)
    print(json.dumps(result, ensure_ascii=False))

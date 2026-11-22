from datetime import datetime
from pathlib import Path

from .schedule_models import SchedulePolicy
from .schedules import ScheduleStore


def add_commands(commands):
    schedule = commands.add_parser("schedule", help="保存和查看作息规则，不启动自动唤醒")
    actions = schedule.add_subparsers(dest="schedule_action", required=True)
    actions.add_parser("show", help="查看读取时的安排与状态")
    configure = actions.add_parser("configure", help="校验并保存 JSON 规则，不开启自动执行")
    configure.add_argument("--file", type=Path, required=True)
    actions.add_parser("pause", help="暂停安排并请求停止当前读信；需确认进程结束")
    actions.add_parser("resume", help="恢复当前时刻的安排，不补跑错过事件")
    override = actions.add_parser("override", help="临时改安排，必须在 24 小时内到期")
    override.add_argument("--phase", choices=["working", "winding_down", "resting"], required=True)
    override.add_argument(
        "--until",
        required=True,
        help="带时区的 ISO 时间，如 2026-10-14T23:00:00+08:00",
    )
    actions.add_parser("clear-override", help="清除临时安排，回到常规作息")


def handle(database, args):
    store = ScheduleStore(database)
    if args.schedule_action == "configure":
        if args.file.stat().st_size > 64 * 1024:
            raise ValueError("Schedule file is too large")
        policy = SchedulePolicy.model_validate_json(args.file.read_bytes())
        store.configure(policy)
    elif args.schedule_action in {"pause", "resume"}:
        store.pause(args.schedule_action == "pause")
    elif args.schedule_action == "override":
        deadline = datetime.fromisoformat(args.until)
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("Temporary schedule deadline must include a timezone")
        store.override(args.phase, int(deadline.timestamp()))
    elif args.schedule_action == "clear-override":
        store.override(None, None)
    print(store.view().model_dump_json())

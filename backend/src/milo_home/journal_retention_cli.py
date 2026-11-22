"""Root-only explicit archival controls, with read-only admission as the default."""

from pathlib import Path

from .codex_journal import PrivateJournal
from .journal_retention import archive, inspect, isolate
from .work_runtime import WorkRuntime


def add_command(actions):
    command = actions.add_parser("storage", help="只读容量／历史准入，或明确封存已交付的执行记录")
    commands = command.add_subparsers(dest="storage_action", required=True)
    for name in ("inspect", "archive"):
        action = commands.add_parser(
            name, help="只读检查" if name == "inspect" else "封存完整历史流，不执行或补发布"
        )
        action.add_argument(
            "--stream", required=name == "archive", help="原stream的32位十六进制标识"
        )
        action.add_argument("--reply-directory", type=Path, help="普通回信原逐轮路由目录")
        if name == "archive":
            action.add_argument("--confirm", action="store_true", required=True)
    quarantine = commands.add_parser(
        "quarantine", help="将未登记孤件复制核验后移到独立目录，保留全部原字节"
    )
    quarantine.add_argument("--out", type=Path, required=True)
    quarantine.add_argument("--confirm", action="store_true", required=True)


def handle(database, directory, args):
    journal = PrivateJournal(
        directory, initialize=False, read_only=args.storage_action == "inspect"
    )
    runtime = WorkRuntime(database, journal)
    if args.storage_action == "quarantine":
        return isolate(runtime, args.out)
    if args.storage_action == "archive":
        return archive(runtime, args.stream, reply_directory=args.reply_directory)
    return inspect(runtime, stream_id=args.stream, reply_directory=args.reply_directory)

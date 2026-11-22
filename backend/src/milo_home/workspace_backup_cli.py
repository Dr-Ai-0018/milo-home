"""Independent offline workspace commands; never load runtime/provider settings."""

import json
from pathlib import Path


def add_commands(commands):
    parser = commands.add_parser("backup-workspace", help="离线备份并在新目录演练Git成果恢复")
    actions = parser.add_subparsers(dest="workspace_action", required=True)
    capture = actions.add_parser("capture", help="停写后保存Git对象、两层修改及明确选中的新文件")
    capture.add_argument("--workspace", type=Path, required=True)
    capture.add_argument("--writers-stopped", action="store_true", required=True)
    capture.add_argument("--include-file", action="append", default=[])
    capture.add_argument("--exclude-file", action="append", default=[])
    rehearse = actions.add_parser("rehearse", help="校验可信摘要，在新私有目录恢复，不接执行器")
    rehearse.add_argument("--archive", type=Path, required=True)
    rehearse.add_argument("--manifest-sha256", required=True)
    for action in (capture, rehearse):
        action.add_argument("--out", type=Path, required=True)
        action.add_argument("--budget-mib", type=int, default=256)


def handle(args):
    from .workspace_backup import capture_workspace, rehearse_workspace

    budget = args.budget_mib * 1024 * 1024
    if args.workspace_action == "capture":
        report = capture_workspace(
            args.workspace,
            args.out,
            include=args.include_file,
            exclude=args.exclude_file,
            writers_stopped=args.writers_stopped,
            budget=budget,
        )
    else:
        report = rehearse_workspace(args.archive, args.out, args.manifest_sha256, budget=budget)
    print(json.dumps(report, ensure_ascii=False))

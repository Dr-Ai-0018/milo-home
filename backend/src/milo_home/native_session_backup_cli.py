"""Native backup commands; readback explicitly starts a metadata-only local peer."""

import json
from pathlib import Path
from uuid import UUID


def add_commands(commands):
    parser = commands.add_parser("backup-native-session", help="备份专用原生会话资料并独立恢复")
    actions = parser.add_subparsers(dest="native_action", required=True)
    capture = actions.add_parser("capture", help="停写后采集原生记录与SQLite，不含认证或配置")
    capture.add_argument("--home", type=Path, required=True)
    capture.add_argument("--thread", type=UUID, required=True)
    capture.add_argument("--writers-stopped", action="store_true", required=True)
    capture.add_argument("--exclude-path", action="append", default=[])
    rehearse = actions.add_parser("rehearse", help="可信摘要核验后恢复文件，不启动Codex")
    rehearse.add_argument("--archive", type=Path, required=True)
    rehearse.add_argument("--manifest-sha256", required=True)
    readback = actions.add_parser("readback", help="在新演练副本验证原生历史，只发元数据请求")
    readback.add_argument("--archive", type=Path, required=True)
    readback.add_argument("--manifest-sha256", required=True)
    readback.add_argument("--cli", type=Path, required=True)
    readback.add_argument("--tool-host", type=Path, required=True)
    readback.add_argument("--timeout", type=int, default=25)
    for action in (capture, rehearse, readback):
        action.add_argument("--out", type=Path, required=True)
        action.add_argument("--budget-mib", type=int, default=256)


def handle(args):
    from .native_session_backup import capture_native, rehearse_native

    budget = args.budget_mib * 1024 * 1024
    if args.native_action == "capture":
        report = capture_native(
            args.home,
            args.out,
            args.thread,
            writers_stopped=args.writers_stopped,
            exclude=args.exclude_path,
            budget=budget,
        )
    elif args.native_action == "readback":
        from .codex_config import CodexRuntime
        from .native_session_readback import readback_native

        report = readback_native(
            args.archive,
            args.out,
            args.manifest_sha256,
            CodexRuntime(args.cli, args.tool_host),
            seconds=args.timeout,
            budget=budget,
        )
    else:
        report = rehearse_native(args.archive, args.out, args.manifest_sha256, budget=budget)
    print(json.dumps(report, ensure_ascii=False))

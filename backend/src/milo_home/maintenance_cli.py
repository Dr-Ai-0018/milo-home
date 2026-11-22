"""Explicit maintenance commands; parsing never reads source or policy files."""

from pathlib import Path


def add_commands(commands):
    command = commands.add_parser("maintenance", help="查看资料维护，或明确执行有限封存和配套备份")
    actions = command.add_subparsers(dest="maintenance_action", required=True)
    actions.add_parser("show", help="只读已发布摘要，不扫描目录或运行维护")
    for name in ("collect", "run"):
        action = actions.add_parser(
            name, help="采集摘要" if name == "collect" else "有限封存并更新摘要"
        )
        action.add_argument("--workspace", type=Path, help="省略时使用部署配置工作区")
        action.add_argument("--backup-directory", type=Path)
        action.add_argument("--development-directory", type=Path)
        action.add_argument("--success-file", type=Path)
        action.add_argument("--daily-unit", help="只读观察的每日备份service或timer名")
        action.add_argument("--reply-directory", type=Path)
        if name == "run":
            action.add_argument("--apply", action="store_true", required=True)
    preview = actions.add_parser("retention-preview", help="只读封存候选与原资格原因")
    preview.add_argument("--reply-directory", type=Path)
    preview.add_argument("--after")
    for name in ("backup-check", "capture"):
        action = actions.add_parser(
            name, help="只读完整采集预检" if name == "backup-check" else "短维护窗口采集完整代次"
        )
        action.add_argument("--workspace", type=Path, help="省略时使用部署配置工作区")
        action.add_argument("--policy", type=Path, required=True)
        action.add_argument("--out", type=Path, required=True)
        action.add_argument("--reply-directory", type=Path)
        if name == "capture":
            action.add_argument("--writers-stopped", action="store_true", required=True)
    rehearse = actions.add_parser("rehearse", help="在独立目录恢复并核对完整代次，不接执行器")
    rehearse.add_argument("--archive", type=Path, required=True)
    rehearse.add_argument("--sha256", required=True)
    rehearse.add_argument("--policy", type=Path, required=True)
    rehearse.add_argument("--out", type=Path, required=True)
    rehearse.add_argument(
        "--record", action="store_true", help="仅原库与本人匹配时记录文件恢复事实"
    )
    restore = actions.add_parser("restore-entries", help="按持久记录恢复本次原入口，不重新采集")
    restore.add_argument("--state-file", type=Path, required=True)
    restore.add_argument("--confirm", action="store_true", required=True)


def handle(args):
    from .maintenance_commands import execute

    execute(args)

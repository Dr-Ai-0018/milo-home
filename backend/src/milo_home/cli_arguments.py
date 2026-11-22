"""Public CLI syntax; constructing help never opens application data."""

import argparse
from datetime import date
from pathlib import Path
from uuid import UUID

from .dev_fixture import SCENARIOS
from .maintenance_cli import add_commands as add_maintenance_commands
from .native_session_backup_cli import add_commands as add_native_backup_commands
from .paired_backup_cli import add_commands as add_paired_backup_commands
from .public_notes_cli import add_commands as add_content_commands
from .public_works_cli import add_commands as add_public_work_commands
from .reception_cli import add_commands as add_reception_commands
from .reply_service_cli import add_commands as add_reply_service_commands
from .revisit_cli import add_commands as add_revisit_commands
from .schedule_cli import add_commands
from .scheduler_cli import add_commands as add_scheduler_commands
from .service_control_cli import add_commands as add_service_commands
from .work_cli import add_commands as add_work_commands
from .workspace_backup_cli import add_commands as add_workspace_backup_commands


def build_parser():
    parser = argparse.ArgumentParser(description="米洛在家的本机开发与管理入口")
    parser.add_argument("--config", type=Path, help="显式部署TOML；也可用MILO_CONFIG选择")
    parser.add_argument("--database", type=Path, help="本次命令覆盖数据库绝对路径")
    parser.add_argument("--origin", action="append", help="本次命令的精确来源，可重复")
    parser.add_argument("--database-user", help="root手动管理时使用的数据库文件属主")
    parser.add_argument("--run-directory", type=Path, help="已有的root私有运行目录")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config-check", help="核对配置字段和来源，不显示值或打开业务库")
    commands.add_parser("init-db", help="建立空数据库或核验已知 schema")
    commands.add_parser("doctor", help="只读检查现有数据库与占用，不建库、迁移或停止进程")
    usage = commands.add_parser("usage", help="只读统计北京时间的自动预约与启动许可，不代表账单")
    usage.add_argument("--day", type=date.fromisoformat, help="日期YYYY-MM-DD，默认北京时间今天")
    preview = commands.add_parser("dev-fixture", help="新建独立合成预览库，不调用模型或启动服务")
    preview.add_argument("--parent", type=Path, default=Path("/tmp"))
    preview.add_argument(
        "--scenario",
        choices=SCENARIOS,
        default="replied",
    )
    schema = commands.add_parser("export-openapi", help="导出公开接口 schema，不初始化数据库")
    schema.add_argument("--out", type=Path, required=True)
    invite = commands.add_parser("invite", help="为一个账号生成一次性邀请，秘密只写入私有文件")
    invite.add_argument("--handle", required=True)
    invite.add_argument("--out", type=Path, required=True)
    backup = commands.add_parser("backup", help="使用 SQLite 在线备份 API 写入新文件")
    backup.add_argument("--out", type=Path, required=True)
    rehearsal = commands.add_parser(
        "backup-rehearse", help="将备份复制到新私有文件重开演练，不替换原库或启动进程"
    )
    rehearsal.add_argument("--file", type=Path, required=True)
    rehearsal.add_argument("--out", type=Path, required=True)
    serve = commands.add_parser("serve", help="在回环地址临时启动 HTTP API")
    serve.add_argument("--port", type=int, default=7484, help="开发 API 端口（默认 7484）")
    wake = commands.add_parser("wake", help="手动读取一批待回信留言并等待结束，不自动重试")
    wake.add_argument("--thread", type=UUID, required=True)
    wake.add_argument("--timeout", type=int, default=3600)
    stop = commands.add_parser("stop", help="持久记录取消请求；执行器确认终止后结束")
    stop.add_argument("--run", type=UUID, required=True)
    inspect = commands.add_parser("run-status", help="查看脱敏运行结果")
    inspect.add_argument("--run", type=UUID, required=True)
    commands.add_parser("recover-runs", help="取得执行锁，停止并确认旧进程后释放遗留占用")
    events = commands.add_parser("events", help="整理或查看持久唤醒事件，不启动执行")
    event_commands = events.add_subparsers(dest="event_action", required=True)
    event_commands.add_parser("plan", help="事务整理事件并列出当前候选，不占用或执行")
    event_commands.add_parser("status", help="只读查看事件计数和扫描游标")
    event_commands.add_parser("reconcile", help="收口已确认终态的运行，不停止或启动进程")
    add_commands(commands)
    add_scheduler_commands(commands)
    add_reply_service_commands(commands)
    add_service_commands(commands)
    add_reception_commands(commands)
    add_revisit_commands(commands)
    add_work_commands(commands)
    add_workspace_backup_commands(commands)
    add_native_backup_commands(commands)
    add_paired_backup_commands(commands)
    add_maintenance_commands(commands)
    add_public_work_commands(add_content_commands(commands))
    return parser

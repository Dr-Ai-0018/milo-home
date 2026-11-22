"""Existing work management and routing of one explicit execution request."""

import json
import os
import threading
from uuid import UUID

from .codex_journal import PrivateJournal
from .database import CURRENT_SCHEMA_VERSION
from .work_access import require_task
from .work_artifacts import publish_artifact
from .work_reconciliation import WorkReconciliation
from .work_recovery import WorkRecovery
from .work_runtime import WorkRuntime
from .work_tasks import WorkTasks


def add_commands(commands):
    from .journal_retention_cli import add_command as add_storage_command
    from .work_execution import add_check_command, add_listen_command, add_run_command
    from .work_setup import add_setup_command

    work = commands.add_parser("work", help="本机管理工作；check纯读，run/listen/take-next可启动")
    actions = work.add_subparsers(dest="work_action", required=True)
    for name, help_ in (
        ("status", "查看任务状态和业务窗口，不输出正文"),
        ("stop", "保存停止请求，不证明执行家族已停止，也不回滚文件"),
        ("inspect", "只读核对原私密证据；历史停止记录不证明现在已停止"),
        ("replay-results", "仅发布已验证的原结果，不重新执行"),
        ("settle", "重新停止原家族并尝试异常收尾；缺证据继续占用"),
    ):
        action = actions.add_parser(name, help=help_)
        action.add_argument("--owner", type=UUID, required=True)
        field = "--task" if name in {"status", "stop"} else "--window"
        action.add_argument(field, type=UUID, required=True)
        if name == "stop":
            action.add_argument("--key", type=UUID, required=True, help="本次停止的幂等标识")
        if name == "settle":
            action.add_argument("--confirm-stop", action="store_true", required=True)
            action.add_argument(
                "--repair-receipts", action="store_true", help="仅用完整原协议补记漏写事实"
            )
            action.add_argument(
                "--native-rollout", help="用原始本机会话文件交叉核对同一轮的上游失败"
            )
    publish = actions.add_parser("publish-artifact", help="登记已核对的单个工作桌文本文件")
    publish.add_argument("--owner", type=UUID, required=True)
    publish.add_argument("--task", type=UUID, required=True)
    publish.add_argument("--path", required=True)
    publish.add_argument("--expected-sha256", required=True)
    add_run_command(actions)
    add_listen_command(actions)
    add_check_command(actions)
    add_setup_command(actions)
    add_storage_command(actions)


def require_management(database, directory):
    if (
        os.geteuid() != 0
        or threading.current_thread() is not threading.main_thread()
        or threading.active_count() != 1
        or directory is None
    ):
        raise ValueError("Work management requires a single-threaded root process and runtime")
    with database.connect() as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0] != CURRENT_SCHEMA_VERSION:
            raise ValueError("Existing current business schema is required")


def status(database, owner, task_id):
    with database.connect() as connection:
        connection.execute("BEGIN")
        task = require_task(connection, owner, task_id)
        windows = connection.execute(
            "SELECT id,state,created_at,deadline_at,ended_at FROM execution_windows "
            "WHERE task_id=? ORDER BY created_at,id",
            (task_id,),
        ).fetchall()
        return {
            "task_status": task["status"],
            "stop_requested": task["stop_requested_at"] is not None,
            "windows": [dict(row) for row in windows],
        }


def handle(database, directory, args):
    require_management(database, directory)
    if args.work_action == "storage":
        from .journal_retention_cli import handle as storage

        result = storage(database, directory, args)
        print(json.dumps(result, ensure_ascii=False))
        if args.storage_action == "archive" and not result["archived"]:
            raise SystemExit(1)
        return
    if args.work_action == "setup":
        from .work_setup import handle as setup_work

        setup_work(database, directory, args)
        return
    if args.work_action in {"run", "listen", "take-next", "check"}:
        from .work_execution import handle as execute_work

        execute_work(database, directory, args)
        return
    owner = str(args.owner)
    action = args.work_action
    if action == "status":
        result = status(database, owner, str(args.task))
    elif action == "publish-artifact":
        result = publish_artifact(database, owner, str(args.task), args.path, args.expected_sha256)
    elif action == "stop":
        task = WorkTasks(database).stop(owner, str(args.task), str(args.key))
        result = {
            "task_status": task["status"],
            "stop_requested": task["stop_requested_at"] is not None,
        }
    else:
        journal = PrivateJournal(directory, initialize=False, read_only=action == "inspect")
        runtime = WorkRuntime(database, journal)
        window = str(args.window)
        if action == "inspect":
            result = WorkRecovery(runtime).inspect(owner, window)
        elif action == "replay-results":
            result = {"known_turns": WorkRecovery(runtime).replay_results(owner, window)}
        else:
            result = WorkReconciliation(runtime).settle(
                owner,
                window,
                repair_receipts=args.repair_receipts,
                native_rollout=args.native_rollout,
            )
        print(json.dumps(result, ensure_ascii=False))
        if action == "settle" and not result["closed"]:
            raise SystemExit(1)
        return
    print(json.dumps(result, ensure_ascii=False))

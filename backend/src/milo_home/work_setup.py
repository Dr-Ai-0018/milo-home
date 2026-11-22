"""Explicit registration and native main setup; never launches a model turn."""

import json
from pathlib import Path
from uuid import UUID

from .codex_config import NewawzConfig, private_read
from .codex_journal import PrivateJournal
from .codex_rpc import ProtocolError
from .execution_budget import ExecutionBudget, ExecutionPolicy, shared_policy
from .execution_lock import execution_lock
from .work_access import WorkAccess
from .work_runtime import WorkRuntime


def add_setup_command(actions):
    from .native_policy_cli import add_command as add_native_policy_command
    from .work_creation import add_commands as add_creation_commands
    from .work_initialization import add_init_command

    setup = actions.add_parser("setup", help="显式登记机器工作能力、政策及已有主绑定；不执行")
    commands = setup.add_subparsers(dest="setup_action", required=True)
    commands.add_parser("show", help="只读查看业务登记状态，不输出路径或私密主对话")
    workspace = commands.add_parser("workspace", help="登记或更新同一工作区；路径不是隔离边界")
    workspace.add_argument("--workspace", type=UUID, required=True)
    workspace.add_argument("--name", required=True)
    workspace.add_argument("--root", type=Path, required=True)
    enabled = workspace.add_mutually_exclusive_group(required=True)
    enabled.add_argument("--enable", dest="enabled", action="store_true")
    enabled.add_argument("--disable", dest="enabled", action="store_false")
    for name, help_ in (
        ("grant", "按真实账号UUID授予本机工作能力；不是显示称呼"),
        ("revoke", "撤销能力并请求停止；不证明进程退出或撤销改动"),
    ):
        command = commands.add_parser(name, help=help_)
        command.add_argument("--owner", type=UUID, required=True)
    policy = commands.add_parser("policy", help="保存共同每日额度；两项必须显式给出")
    policy.add_argument("--file", type=Path, required=True)
    binding = commands.add_parser("bind-main", help="仅关联已有业务会话与已登记的私密主对话")
    binding.add_argument("--owner", type=UUID, required=True)
    binding.add_argument("--session", type=UUID, required=True)
    add_init_command(commands)
    add_creation_commands(commands)
    add_native_policy_command(commands)


def show(database):
    with database.connect() as connection:
        connection.execute("BEGIN")
        workspaces = connection.execute(
            "SELECT id,name,enabled,revision FROM workspaces ORDER BY id"
        ).fetchall()
        operators = connection.execute(
            "SELECT user_id,enabled,revision FROM work_access ORDER BY user_id"
        ).fetchall()
        sessions = connection.execute(
            "SELECT id,owner_id,workspace_id,EXISTS (SELECT 1 FROM work_runtime_bindings "
            "WHERE session_id=work_sessions.id) AS association_recorded FROM work_sessions"
        ).fetchall()
        policy = shared_policy(connection)
        return {
            "workspaces": [dict(row) for row in workspaces],
            "operators": [dict(row) for row in operators],
            "sessions": [dict(row) for row in sessions],
            "policy": policy.model_dump() if policy else None,
            "execution_available": False,
        }


def bind_main(database, directory, owner, session_id):
    with execution_lock(database) as lease:
        journal = PrivateJournal(directory, initialize=False)
        config = NewawzConfig.load()
        installed = journal.installation()
        installed.verify()
        journal.bind(journal.binding(), config, installed)
        if journal.outstanding():
            raise ProtocolError("Previous private execution still needs reconciliation")
        binding = WorkRuntime(database, journal).bind(owner, session_id, lease=lease)
        return {"session_id": binding.session_id, "association_recorded": True}


def handle(database, directory, args):
    action = args.setup_action
    access = WorkAccess(database)
    if action == "show":
        report = show(database)
    elif action == "workspace":
        workspace = str(args.workspace)
        access.register_workspace(workspace, args.name, args.root, enabled=args.enabled)
        report = {"workspace_id": workspace, "enabled": args.enabled}
    elif action in {"grant", "revoke"}:
        owner = str(args.owner)
        if action == "grant":
            access.grant(owner)
        else:
            access.revoke(owner)
        report = {"owner_id": owner, "enabled": action == "grant"}
    elif action == "policy":
        policy = ExecutionPolicy.model_validate_json(private_read(args.file))
        ExecutionBudget(database).configure(policy)
        report = {"policy": policy.model_dump()}
    elif action == "bind-main":
        report = bind_main(database, directory, str(args.owner), str(args.session))
    elif action == "native-policy":
        from .native_policy_cli import handle as handle_native_policy

        report = handle_native_policy(database, directory, args)
    elif action == "init-main":
        from .codex_config import CodexRuntime
        from .work_initialization import initialize_main

        report = initialize_main(
            database,
            directory,
            str(args.owner),
            str(args.session),
            str(args.thread),
            CodexRuntime(args.cli, args.tool_host),
        )
    elif action in {"create-main", "create-main-successor", "recover-main", "abandon-main"}:
        from .work_creation import handle as create_main

        report = create_main(database, directory, args)
    else:
        raise ValueError("Unknown registration command")
    print(json.dumps(report, ensure_ascii=False))

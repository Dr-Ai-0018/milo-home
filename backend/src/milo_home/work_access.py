"""Explicit operator/workspace registration; never infer authority from message text."""

import time
from pathlib import Path
from uuid import UUID

from .errors import ApiError


def unavailable():
    return ApiError(404, "work_not_found", "没有找到可访问的工作记录。")


def require_access(connection, owner: str, workspace: str):
    row = connection.execute(
        "SELECT work_access.revision AS owner_revision, workspaces.revision AS workspace_revision "
        "FROM work_access CROSS JOIN workspaces "
        "WHERE user_id=? AND work_access.enabled=1 AND workspaces.id=? AND workspaces.enabled=1",
        (owner, workspace),
    ).fetchone()
    if row is None:
        raise unavailable()
    return row


def require_session(connection, owner: str, session_id: str):
    row = connection.execute(
        "SELECT * FROM work_sessions WHERE id=? AND owner_id=?", (session_id, owner)
    ).fetchone()
    if row is None:
        raise unavailable()
    require_access(connection, owner, row["workspace_id"])
    return row


def require_task(connection, owner: str, task_id: str):
    row = connection.execute(
        "SELECT * FROM work_tasks WHERE id=? AND owner_id=?", (task_id, owner)
    ).fetchone()
    if row is None:
        raise unavailable()
    access = require_access(connection, owner, row["workspace_id"])
    if any(row[key] != access[key] for key in ["owner_revision", "workspace_revision"]):
        raise unavailable()
    return row


def stop_tasks(connection, now: int, *, owner=None, workspace=None):
    if not connection.in_transaction or (owner is None) == (workspace is None):
        raise ValueError("Stop scope requires one identity and a write transaction")
    column, value = ("owner_id", owner) if owner is not None else ("workspace_id", workspace)
    connection.execute(
        "UPDATE work_tasks SET status=CASE WHEN status='needs_reconciliation' THEN status "
        "WHEN status='queued' AND NOT EXISTS "
        "(SELECT 1 FROM execution_windows WHERE task_id=work_tasks.id AND state!='closed') "
        "THEN 'cancelled' ELSE 'stopping' END, "
        "stop_requested_at=coalesce(stop_requested_at,?), updated_at=? "
        f"WHERE {column}=? AND status IN ('queued','running','stopping','needs_reconciliation')",
        (now, now, value),
    )
    connection.execute(
        "UPDATE work_inputs SET delivery='cancelled' WHERE delivery='queued' AND task_id IN "
        f"(SELECT id FROM work_tasks WHERE {column}=? AND stop_requested_at IS NOT NULL)",
        (value,),
    )
    connection.execute(
        "UPDATE execution_windows SET state=CASE WHEN state='unknown' "
        "THEN state ELSE 'stopping' END, "
        "stop_reason=coalesce(stop_reason,'access_changed') WHERE state!='closed' AND task_id IN "
        f"(SELECT id FROM work_tasks WHERE {column}=?)",
        (value,),
    )


class WorkAccess:
    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock

    def register_workspace(self, workspace_id: str, name: str, root: Path, *, enabled=True):
        workspace_id = str(UUID(workspace_id))
        if not root.is_absolute() or root.resolve() != root or not root.is_dir():
            raise ValueError("Workspace must be an existing canonical absolute directory")
        name = name.strip()
        if not 1 <= len(name) <= 80 or type(enabled) is not bool:
            raise ValueError("Invalid workspace registration")
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            previous = connection.execute("SELECT * FROM workspaces").fetchone()
            if previous is None:
                connection.execute(
                    "INSERT INTO workspaces VALUES (?,1,?,?,?,1,?)",
                    (workspace_id, name, str(root), int(enabled), now),
                )
                return
            if previous["id"] != workspace_id:
                raise ApiError(409, "main_workspace_bound", "主工作区已登记，不能自动替换。")
            changed = previous["root_path"] != str(root) or bool(previous["enabled"]) != enabled
            if changed:
                stop_tasks(connection, now, workspace=workspace_id)
            connection.execute(
                "UPDATE workspaces SET name=?,root_path=?,enabled=?,"
                "revision=revision+?,updated_at=? "
                "WHERE id=?",
                (name, str(root), int(enabled), int(changed), now, workspace_id),
            )

    def grant(self, owner: str):
        owner = str(UUID(owner))
        with self.database.connect(write=True) as connection:
            if not connection.execute("SELECT 1 FROM users WHERE id=?", (owner,)).fetchone():
                raise unavailable()
            if connection.execute(
                "SELECT 1 FROM work_sessions WHERE owner_id!=? "
                "UNION ALL SELECT 1 FROM work_access WHERE user_id!=? AND enabled=1",
                (owner, owner),
            ).fetchone():
                raise ApiError(409, "main_operator_bound", "主对话已有操作者，不能自动转交。")
            connection.execute(
                "INSERT INTO work_access VALUES (?,1,1,?) ON CONFLICT(user_id) DO UPDATE SET "
                "enabled=1,revision=revision+CASE WHEN enabled=0 THEN 1 ELSE 0 END,updated_at=?",
                (owner, int(self.clock()), int(self.clock())),
            )

    def revoke(self, owner: str):
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            changed = connection.execute(
                "UPDATE work_access SET enabled=0,revision=revision+1,updated_at=? "
                "WHERE user_id=? AND enabled=1",
                (now, owner),
            ).rowcount
            if changed:
                stop_tasks(connection, now, owner=owner)

    def workspaces(self, owner: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,name FROM workspaces WHERE enabled=1 AND EXISTS "
                "(SELECT 1 FROM work_access WHERE user_id=? AND enabled=1)",
                (owner,),
            ).fetchall()
            return [dict(row) for row in rows]

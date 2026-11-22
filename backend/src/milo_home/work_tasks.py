"""Idempotent work intake; accepting text never starts an execution window."""

import time
from uuid import uuid4

from .errors import ApiError
from .execution_permits import active_window
from .execution_pulse import PULSE_TTL
from .reception import observe, on_new_input
from .schemas import MessageInput
from .work_access import require_access, require_session, require_task
from .work_decisions import (
    SUSPENDED_SQL,
    cancel_revisits,
    clarification_allowed,
    decision_view,
    history_clause,
    record_clarification,
    stop_suspended,
)
from .work_deliverables import deliverable_view, expected_deliverable
from .work_models import CreateWorkInput, WorkTaskStatusGroup
from .work_steer import accepting_turn

TASK_FIELDS = (
    "id",
    "session_id",
    "sequence",
    "status",
    "created_at",
    "updated_at",
    "stop_requested_at",
)
INPUT_FIELDS = ("id", "task_id", "sequence", "kind", "body", "delivery", "created_at")


def input_view(row):
    return {key: row[key] for key in INPUT_FIELDS} | {
        "active_turn": row["active_turn"] if "active_turn" in row.keys() else None,
        "source": row["source"] if "source" in row.keys() else "visitor",
    }


def task_view(connection, row, now=None):
    result = {key: row[key] for key in TASK_FIELDS}
    initial = connection.execute(
        "SELECT * FROM work_inputs WHERE task_id=? AND sequence=1", (row["id"],)
    ).fetchone()
    result["initial_input"] = input_view(initial)
    result["deliverable"] = deliverable_view(connection, row["id"])
    active = accepting_turn(connection, row["id"], now=now) if row["status"] == "running" else None
    result["active_turn"] = active["id"] if active is not None else None
    result["execution_available"] = False
    result["decision"] = decision_view(connection, row["id"])
    return result


def session_view(connection, row, now):
    return {key: row[key] for key in ["id", "workspace_id", "created_at"]} | {
        "execution_available": False,
        "reception": observe(connection, row["owner_id"], "work", now, now + PULSE_TTL),
    }


def previous_input(
    connection, owner, key, *, body, kind, task_id=None, session_id=None, active_turn=None
):
    previous = connection.execute(
        "SELECT work_inputs.*, work_tasks.session_id, s.permit_id AS active_turn FROM work_inputs "
        "JOIN work_tasks ON work_tasks.id=work_inputs.task_id "
        "LEFT JOIN work_steer_inputs s ON s.input_id=work_inputs.id "
        "WHERE work_inputs.owner_id=? AND client_key=?",
        (owner, key),
    ).fetchone()
    if previous is not None:
        if (
            previous["body"] != body
            or previous["kind"] != kind
            or (task_id is not None and previous["task_id"] != task_id)
            or (session_id is not None and previous["session_id"] != session_id)
            or previous["active_turn"] != active_turn
        ):
            raise ApiError(409, "idempotency_conflict", "请求标识已用于另一项工作输入。")
        require_task(connection, owner, previous["task_id"])
    return previous


def insert_input(connection, owner, task_id, body, key, kind, sequence, now):
    input_id = str(uuid4())
    connection.execute(
        "INSERT INTO work_inputs VALUES (?,?,?,?,?,?,?,'queued',?)",
        (input_id, task_id, owner, sequence, kind, body, key, now),
    )
    on_new_input(connection, owner, "work", now)
    return connection.execute("SELECT * FROM work_inputs WHERE id=?", (input_id,)).fetchone()


class WorkTasks:
    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock

    def main_session(self, owner: str, workspace: str):
        with self.database.connect(write=True) as connection:
            require_access(connection, owner, workspace)
            row = connection.execute("SELECT * FROM work_sessions").fetchone()
            if row is not None:
                if row["owner_id"] != owner or row["workspace_id"] != workspace:
                    raise ApiError(409, "main_session_bound", "主会话已经绑定，不能自动替换。")
                return session_view(connection, row, int(self.clock())), False
            session_id = str(uuid4())
            connection.execute(
                "INSERT INTO work_sessions VALUES (?,1,?,?,?)",
                (session_id, owner, workspace, int(self.clock())),
            )
            return session_view(
                connection, require_session(connection, owner, session_id), int(self.clock())
            ), True

    def session(self, owner: str, session_id: str):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            return session_view(
                connection, require_session(connection, owner, session_id), int(self.clock())
            )

    def create(self, owner: str, session_id: str, body: str, key: str, deliverable_name=None):
        body = CreateWorkInput(body=body, deliverable_name=deliverable_name).body
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            session = require_session(connection, owner, session_id)
            access = require_access(connection, owner, session["workspace_id"])
            previous = previous_input(
                connection, owner, key, body=body, kind="initial", session_id=session_id
            )
            if previous is not None:
                expected = expected_deliverable(connection, previous["task_id"])
                if (expected["filename"] if expected else None) != deliverable_name:
                    raise ApiError(409, "idempotency_conflict", "请求标识已用于另一份交付要求。")
                return task_view(
                    connection, require_task(connection, owner, previous["task_id"])
                ), False
            count = connection.execute(
                "SELECT count(*) FROM work_tasks WHERE owner_id=? "
                "AND (status IN ('queued','running','stopping','needs_reconciliation') "
                "OR (status='completed' AND " + SUSPENDED_SQL + "))",
                (owner,),
            ).fetchone()[0]
            if count >= 32:
                raise ApiError(429, "work_queue_full", "待处理工作较多，请先完成或取消一些。")
            sequence = connection.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM work_tasks WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            task_id = str(uuid4())
            connection.execute(
                "INSERT INTO work_tasks VALUES (?,?,?,?,?,?,?,'queued',?,?,NULL)",
                (
                    task_id,
                    session_id,
                    owner,
                    session["workspace_id"],
                    access["owner_revision"],
                    access["workspace_revision"],
                    sequence,
                    now,
                    now,
                ),
            )
            insert_input(connection, owner, task_id, body, key, "initial", 1, now)
            if deliverable_name is not None:
                connection.execute(
                    "INSERT INTO work_deliverables (task_id,filename) VALUES (?,?)",
                    (task_id, deliverable_name),
                )
            return task_view(connection, require_task(connection, owner, task_id)), True

    def get(self, owner: str, task_id: str):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            row = require_task(connection, owner, task_id)
            return task_view(connection, row, int(self.clock()))

    def list(self, owner: str, session_id: str, after: int, limit: int):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            session = require_session(connection, owner, session_id)
            access = require_access(connection, owner, session["workspace_id"])
            rows = connection.execute(
                "SELECT * FROM work_tasks WHERE session_id=? AND owner_revision=? "
                "AND workspace_revision=? AND sequence>? ORDER BY sequence LIMIT ?",
                (session_id, access["owner_revision"], access["workspace_revision"], after, limit),
            ).fetchall()
            return {
                "tasks": [task_view(connection, row, int(self.clock())) for row in rows],
                "next_after": rows[-1]["sequence"] if rows else after,
            }

    def history(
        self,
        owner: str,
        session_id: str,
        before: int | None,
        limit: int,
        status_group: WorkTaskStatusGroup = "all",
    ):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            session = require_session(connection, owner, session_id)
            access = require_access(connection, owner, session["workspace_id"])
            scope = (session_id, access["owner_revision"], access["workspace_revision"])
            status_clause = "AND " + history_clause(status_group) + " "
            boundary = "AND sequence<? " if before is not None else ""
            cursor = () if before is None else (before,)
            rows = connection.execute(
                "SELECT * FROM work_tasks WHERE session_id=? AND owner_revision=? "
                "AND workspace_revision=? "
                + status_clause
                + boundary
                + "ORDER BY sequence DESC LIMIT ?",
                (*scope, *cursor, limit + 1),
            ).fetchall()
            now = int(self.clock())
            return {
                "tasks": [task_view(connection, row, now) for row in rows[:limit]],
                "next_before": rows[limit - 1]["sequence"] if len(rows) > limit else None,
            }

    def append(self, owner: str, task_id: str, body: str, key: str, active_turn=None):
        body = MessageInput(body=body).body
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            task = require_task(connection, owner, task_id)
            previous = previous_input(
                connection,
                owner,
                key,
                body=body,
                kind="supplement",
                task_id=task_id,
                active_turn=active_turn,
            )
            if previous is not None:
                return input_view(previous), False
            if active_turn is not None:
                active = accepting_turn(connection, task_id, now=now)
                if active is None or active["id"] != active_turn:
                    raise ApiError(409, "work_turn_changed", "这一轮已不能补话，文字还留在这里。")
                active_window(connection, owner, active["window_id"], now)
            elif task["status"] != "queued" and not clarification_allowed(connection, task):
                raise ApiError(409, "work_input_conflict", "任务状态已变化，补充内容尚未投递。")
            sequence = connection.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM work_inputs WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            if sequence > 100:
                raise ApiError(429, "work_inputs_full", "本项工作输入较多，请先整理任务。")
            row = insert_input(connection, owner, task_id, body, key, "supplement", sequence, now)
            if task["status"] == "completed":
                record_clarification(connection, task, row["id"], now)
            if active_turn is not None:
                connection.execute(
                    "INSERT INTO work_steer_inputs VALUES (?,?)", (row["id"], active_turn)
                )
            connection.execute("UPDATE work_tasks SET updated_at=? WHERE id=?", (now, task_id))
            return input_view(row) | {"active_turn": active_turn}, True

    def inputs(self, owner: str, task_id: str, after: int, limit: int):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            require_task(connection, owner, task_id)
            rows = connection.execute(
                "SELECT i.*, s.permit_id AS active_turn,CASE WHEN v.input_id IS NULL "
                "THEN 'visitor' ELSE 'revisit' END AS source FROM work_inputs i "
                "LEFT JOIN work_steer_inputs s ON s.input_id=i.id "
                "LEFT JOIN work_revisit_inputs v ON v.input_id=i.id "
                "WHERE i.task_id=? AND i.sequence>? ORDER BY i.sequence LIMIT ?",
                (task_id, after, limit),
            ).fetchall()
            return {
                "inputs": [input_view(row) for row in rows],
                "next_after": rows[-1]["sequence"] if rows else after,
            }

    def stop(self, owner: str, task_id: str, key: str):
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            task = require_task(connection, owner, task_id)
            previous = connection.execute(
                "SELECT task_id FROM work_stops WHERE owner_id=? AND client_key=?", (owner, key)
            ).fetchone()
            if previous is not None and previous[0] != task_id:
                raise ApiError(409, "idempotency_conflict", "请求标识已用于另一项工作停止。")
            if previous is None:
                connection.execute(
                    "INSERT INTO work_stops VALUES (?,?,?,?)", (owner, key, task_id, now)
                )
            stop_suspended(connection, task, now)
            cancel_revisits(connection, task_id, now)
            if task["status"] in {"queued", "running", "stopping", "needs_reconciliation"}:
                occupied = connection.execute(
                    "SELECT 1 FROM execution_windows WHERE task_id=? AND state!='closed'",
                    (task_id,),
                ).fetchone()
                status = task["status"]
                if status != "needs_reconciliation":
                    status = "cancelled" if status == "queued" and not occupied else "stopping"
                connection.execute(
                    "UPDATE work_tasks SET status=?,"
                    "stop_requested_at=coalesce(stop_requested_at,?), "
                    "updated_at=CASE WHEN stop_requested_at IS NULL THEN ? ELSE updated_at END "
                    "WHERE id=?",
                    (status, now, now, task_id),
                )
                connection.execute(
                    "UPDATE work_inputs SET delivery='cancelled' "
                    "WHERE task_id=? AND delivery='queued'",
                    (task_id,),
                )
                connection.execute(
                    "UPDATE execution_windows SET state=CASE WHEN state='unknown' THEN state "
                    "ELSE 'stopping' END,stop_reason=coalesce(stop_reason,'cancelled') "
                    "WHERE task_id=? AND state!='closed'",
                    (task_id,),
                )
            return task_view(connection, require_task(connection, owner, task_id))

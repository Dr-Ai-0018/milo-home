"""Claim a sourced work resumption without clearing any original delivery or permit."""

from uuid import uuid4

from .errors import ApiError
from .work_decisions import latest


def ready_task(connection, task, now, *, decisions):
    decision = latest(connection, task["id"])
    if task["stop_requested_at"] is not None:
        raise ApiError(409, "work_state_changed", "任务已停止，不能再次领取。")
    if decision is not None and not decisions:
        raise ApiError(
            409, "work_decision_unsupported", "本入口不能处理新的交办决定，请使用原生接续入口。"
        )
    if task["status"] == "queued":
        if decision is None:
            return None
        if (
            decision["state"] == "needs_clarification"
            and connection.execute(
                "SELECT 1 FROM work_continuations c JOIN work_inputs i ON i.id=c.input_id "
                "WHERE c.task_id=? AND c.decision_revision=? AND i.delivery='queued'",
                (task["id"], decision["revision"]),
            ).fetchone()
        ):
            return None
    if task["status"] == "completed" and decision is not None and decision["state"] == "deferred":
        revisit = connection.execute(
            "SELECT * FROM work_revisits WHERE id=? AND state IN ('scheduled','blocked') "
            "AND due_at<=? AND updated_at<=?",
            (decision["revisit_id"], now, now),
        ).fetchone()
        if revisit is not None:
            return revisit
    raise ApiError(409, "work_state_changed", "任务状态或恢复安排已变化，不能预约窗口。")


def claim_due(connection, task, revisit, window_id, now):
    input_ = connection.execute(
        "SELECT * FROM work_inputs WHERE task_id=? AND delivery='queued' "
        "AND NOT EXISTS (SELECT 1 FROM work_steer_inputs s WHERE s.input_id=work_inputs.id) "
        "ORDER BY sequence LIMIT 1",
        (task["id"],),
    ).fetchone()
    if input_ is None:
        sequence = connection.execute(
            "SELECT coalesce(max(sequence),0)+1 FROM work_inputs WHERE task_id=?", (task["id"],)
        ).fetchone()[0]
        if sequence > 100:
            raise ApiError(409, "work_inputs_full", "这张单的轮次已较多，请先核对安排。")
        input_id = str(uuid4())
        connection.execute(
            "INSERT INTO work_inputs VALUES (?,?,?,?, 'supplement',?,?,'queued',?)",
            (
                input_id,
                task["id"],
                task["owner_id"],
                sequence,
                "按已保存的暂缓安排重新查看这件交办；本条由服务触发，没有新增访客留言。",
                "work-revisit:" + revisit["id"] + ":" + str(revisit["revision"]),
                now,
            ),
        )
        connection.execute(
            "INSERT INTO work_revisit_inputs VALUES (?,?,?,?)",
            (input_id, revisit["id"], revisit["revision"], revisit["due_at"]),
        )
    else:
        input_id = input_["id"]
    decision = latest(connection, task["id"])
    connection.execute(
        "INSERT INTO work_revisit_claims VALUES (?,?,?,?,?,?,?)",
        (
            window_id,
            revisit["id"],
            revisit["revision"],
            input_id,
            decision["revision"],
            revisit["due_at"],
            now,
        ),
    )
    connection.execute(
        "UPDATE work_revisits SET state='running',blocked_reason=NULL,updated_at=? WHERE id=?",
        (now, revisit["id"]),
    )
    connection.execute(
        "UPDATE work_tasks SET status='queued',updated_at=? WHERE id=?", (now, task["id"])
    )


def input_source(connection, window_id, input_id):
    row = connection.execute(
        "SELECT c.*,v.origin_result_id,EXISTS(SELECT 1 FROM work_revisit_inputs i "
        "WHERE i.input_id=c.input_id) AS internal FROM work_revisit_claims c "
        "JOIN work_revisits v ON v.id=c.revisit_id WHERE c.window_id=? AND c.input_id=?",
        (window_id, input_id),
    ).fetchone()
    if row is None:
        return None
    return {
        key: row[key]
        for key in (
            "revisit_id",
            "revision",
            "due_at",
            "decision_revision",
            "origin_result_id",
            "internal",
        )
    }


def settle_window(connection, window_id, now, *, unknown=False, outcome=None):
    row = connection.execute(
        "SELECT * FROM work_revisit_claims WHERE window_id=?", (window_id,)
    ).fetchone()
    if row is None:
        return
    if unknown:
        connection.execute(
            "UPDATE work_revisits SET state='needs_reconciliation',"
            "blocked_reason=NULL,updated_at=? "
            "WHERE id=? AND revision=? AND state IN ('running','needs_reconciliation')",
            (now, row["revisit_id"], row["revision"]),
        )
    elif outcome is not None and outcome != "completed":
        connection.execute(
            "UPDATE work_revisits SET state='cancelled',revision=revision+1,"
            "blocked_reason=NULL,updated_at=? "
            "WHERE id=? AND revision=? AND state IN ('running','needs_reconciliation')",
            (now, row["revisit_id"], row["revision"]),
        )


def complete_source(connection, result, now):
    row = connection.execute(
        "SELECT * FROM work_revisit_claims WHERE window_id=? AND input_id=?",
        (result["window_id"], result["input_id"]),
    ).fetchone()
    if row is None:
        return
    if (
        connection.execute(
            "UPDATE work_revisits SET state='completed',revision=revision+1,updated_at=? "
            "WHERE id=? AND revision=? AND state IN ('running','needs_reconciliation')",
            (now, row["revisit_id"], row["revision"]),
        ).rowcount
        != 1
    ):
        raise ValueError("Original work revisit claim changed before publication")

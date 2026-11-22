"""Read the task-level receipt from already published business evidence."""

import json

from .work_access import require_task
from .work_decisions import decision_view
from .work_output_models import WorkOutputView

PENDING = {"state": "pending", "text": None}
UNAVAILABLE = {"state": "unavailable", "text": None}
STATUS_TEXT = {
    "failed": "这次处理失败，未能确认交办目标已完成。可查看下方逐轮记录。",
    "cancelled": "这次处理已取消。取消不代表已撤销此前的操作。",
    "timed_out": "这次处理超过期限，未能确认交办目标已完成。",
    "needs_reconciliation": "这次处理还需要核对，目前无法确认最终结果。",
}


def available(text):
    return {"state": "available", "text": text}


def read_receipt(database, owner, task_id):
    with database.connect() as connection:
        connection.execute("BEGIN")
        task = require_task(connection, owner, task_id)
        enrolled = connection.execute(
            "SELECT 1 FROM work_receipt_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        status = task["status"]
        decision = decision_view(connection, task_id)
        if status == "completed" and decision is not None:
            return available(decision["receipt"])
        if enrolled is None:
            return UNAVAILABLE
        if status in {"queued", "running", "stopping"}:
            return PENDING
        if status in STATUS_TEXT:
            return available(STATUS_TEXT[status])
        turns = connection.execute(
            "SELECT r.id, r.outcome, r.output FROM work_inputs i "
            "JOIN execution_permits p ON p.input_id=i.id AND p.launched_at IS NOT NULL "
            "LEFT JOIN work_results r ON r.permit_id=p.id "
            "WHERE i.task_id=? ORDER BY i.sequence",
            (task_id,),
        ).fetchall()
        if any(turn["id"] is None for turn in turns):
            return PENDING
        final = turns[-1] if turns else None
        if final is not None and final["outcome"] == "completed" and final["output"] is not None:
            answer = WorkOutputView.model_validate(json.loads(final["output"])).answer
            if answer.state == "available":
                return available("这次处理已结束。最后一轮的答复：\n\n" + answer.body)
        return available("这次处理已结束，但没有可展示的最终答复。可查看下方逐轮记录。")

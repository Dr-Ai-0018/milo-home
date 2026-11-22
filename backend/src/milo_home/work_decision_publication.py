"""Apply verified personal work choices only after the original window has stopped."""

import json
from uuid import NAMESPACE_URL, uuid5

from .work_decision_result import WorkDecisionResult
from .work_decisions import latest
from .work_reception import publish_reception
from .work_revisits import complete_source


def publish_decision(connection, result, payload, now):
    request = WorkDecisionResult.model_validate(payload)
    request.require_time(result["observed_at"])
    if result["outcome"] != "completed" or now < result["observed_at"]:
        raise ValueError("Work decision requires its original completed turn")
    encoded = json.dumps(request.model_dump(), ensure_ascii=False, sort_keys=True)
    original = connection.execute(
        "SELECT * FROM work_choice_receipts WHERE result_id=?", (result["id"],)
    ).fetchone()
    if original is not None:
        if original["payload"] != encoded:
            raise ValueError("Published work decision differs from its original receipt")
        return False
    previous = connection.execute(
        "SELECT * FROM work_decisions WHERE result_id=?", (result["id"],)
    ).fetchone()
    if previous is not None:
        due = connection.execute(
            "SELECT initial_due_at FROM work_revisits WHERE origin_result_id=?", (result["id"],)
        ).fetchone()
        if (
            previous["state"] != request.decision
            or previous["receipt"] != request.receipt
            or previous["decided_at"] != result["observed_at"]
            or (due[0] if due else None) != request.revisit_at
        ):
            raise ValueError("Published work decision differs from its original receipt")
        return False
    window = connection.execute(
        "SELECT * FROM execution_windows WHERE id=?", (result["window_id"],)
    ).fetchone()
    task = connection.execute(
        "SELECT * FROM work_tasks WHERE id=?", (result["task_id"],)
    ).fetchone()
    if window["state"] != "closed":
        return False
    apply = window["stop_reason"] is None and task["stop_requested_at"] is None
    connection.execute(
        "INSERT INTO work_choice_receipts VALUES (?,?,?)", (result["id"], encoded, int(apply))
    )
    if not apply:
        return False
    current = latest(connection, result["task_id"])
    if current is not None:
        sequence = connection.execute(
            "SELECT i.sequence FROM work_results r JOIN work_inputs i ON i.id=r.input_id "
            "WHERE r.id=?",
            (current["result_id"],),
        ).fetchone()[0]
        incoming = connection.execute(
            "SELECT sequence FROM work_inputs WHERE id=?", (result["input_id"],)
        ).fetchone()[0]
        if incoming <= sequence:
            raise ValueError("Late work result cannot replace a later personal choice")
    revisit_id = None
    if request.decision == "deferred":
        revisit_id = str(uuid5(NAMESPACE_URL, "milo-home.work-revisit:" + result["id"]))
        connection.execute(
            "INSERT INTO work_revisits "
            "(id,task_id,origin_result_id,state,due_at,initial_due_at,created_at,updated_at) "
            "VALUES (?,?,?,'scheduled',?,?,?,?)",
            (
                revisit_id,
                result["task_id"],
                result["id"],
                request.revisit_at,
                request.revisit_at,
                result["observed_at"],
                now,
            ),
        )
    connection.execute(
        "INSERT INTO work_decisions VALUES (?,?,?,?,?,?,?)",
        (
            result["task_id"],
            current["revision"] + 1 if current else 1,
            result["id"],
            request.decision,
            request.receipt,
            result["observed_at"],
            revisit_id,
        ),
    )
    if request.reception is not None:
        publish_reception(connection, result, request.reception, now)
    complete_source(connection, result, now)
    return True

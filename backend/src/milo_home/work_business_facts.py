"""Freeze mutable per-window observations; always re-read immutable result origins."""

import json

from .journal_retention_facts import one, rows


def window_observation(connection, task_id):
    return {
        "task": one(connection, "SELECT * FROM work_tasks WHERE id=?", (task_id,)),
        "deliverable": one(
            connection, "SELECT * FROM work_deliverables WHERE task_id=?", (task_id,)
        ),
        "artifacts": rows(
            connection, "SELECT * FROM work_artifacts WHERE task_id=? ORDER BY path", (task_id,)
        ),
        "attachment_batch": one(
            connection, "SELECT * FROM work_attachment_batches WHERE task_id=?", (task_id,)
        ),
        "attachments": rows(
            connection, "SELECT * FROM work_attachments WHERE task_id=? ORDER BY id", (task_id,)
        ),
        "receipt_task": one(
            connection, "SELECT * FROM work_receipt_tasks WHERE task_id=?", (task_id,)
        ),
    }


def freeze_window(connection, window_id):
    if connection.execute(
        "SELECT 1 FROM work_window_facts WHERE window_id=?", (window_id,)
    ).fetchone():
        return
    window = one(connection, "SELECT * FROM execution_windows WHERE id=?", (window_id,))
    if window["state"] != "closed" or window["stop_reason"] is not None:
        return
    results = rows(
        connection,
        "SELECT r.id,c.applied FROM work_results r "
        "LEFT JOIN work_choice_receipts c ON c.result_id=r.id "
        "WHERE r.window_id=?",
        (window_id,),
    )
    if not results or any(row["applied"] != 1 for row in results):
        return
    snapshot = window_observation(connection, window["task_id"])
    if snapshot["task"]["status"] != "completed":
        return
    connection.execute(
        "INSERT INTO work_window_facts VALUES (?,?)",
        (window_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True)),
    )


def with_choice_context(connection, snapshot, facts):
    if not any(
        event.get("kind") == "work_terminal" and "decision" in event
        for _, event in snapshot["entries"]
    ):
        return facts
    window_id = facts["window"]["id"]
    saved = one(connection, "SELECT * FROM work_window_facts WHERE window_id=?", (window_id,))
    if saved is not None:
        facts.update(json.loads(saved["payload"]))
    facts["inputs"] = rows(
        connection,
        "SELECT i.* FROM work_inputs i WHERE EXISTS "
        "(SELECT 1 FROM execution_permits p WHERE p.input_id=i.id AND p.window_id=?) "
        "OR EXISTS (SELECT 1 FROM work_steer_inputs s JOIN execution_permits p ON p.id=s.permit_id "
        "WHERE s.input_id=i.id AND p.window_id=?) ORDER BY i.sequence",
        (window_id, window_id),
    )
    facts["steer_turns"] = rows(
        connection,
        "SELECT s.* FROM work_steer_turns s JOIN execution_permits p ON p.id=s.permit_id "
        "WHERE p.window_id=? ORDER BY s.permit_id",
        (window_id,),
    )
    facts["steer_inputs"] = rows(
        connection,
        "SELECT s.* FROM work_steer_inputs s JOIN execution_permits p ON p.id=s.permit_id "
        "WHERE p.window_id=? ORDER BY s.input_id",
        (window_id,),
    )
    queries = {
        "receipts": (
            "SELECT c.* FROM work_choice_receipts c JOIN work_results r ON r.id=c.result_id "
            "WHERE r.window_id=? ORDER BY c.result_id"
        ),
        "decisions": (
            "SELECT d.* FROM work_decisions d JOIN work_results r ON r.id=d.result_id "
            "WHERE r.window_id=? ORDER BY d.result_id"
        ),
        "revisits": (
            "SELECT v.id,v.task_id,v.origin_result_id,v.initial_due_at,v.created_at "
            "FROM work_revisits v JOIN work_results r ON r.id=v.origin_result_id "
            "WHERE r.window_id=? ORDER BY v.id"
        ),
        "bases": (
            "SELECT b.* FROM work_turn_bases b JOIN execution_permits p ON p.id=b.permit_id "
            "WHERE p.window_id=? ORDER BY b.permit_id"
        ),
        "receptions": (
            "SELECT c.*,m.result_id FROM work_result_receptions m JOIN reception_changes c "
            "ON c.owner_id=m.owner_id AND c.revision=m.revision "
            "JOIN work_results r ON r.id=m.result_id WHERE r.window_id=? ORDER BY m.result_id"
        ),
        "claims": "SELECT * FROM work_revisit_claims WHERE window_id=?",
        "internal_inputs": (
            "SELECT v.* FROM work_revisit_inputs v "
            "JOIN execution_permits p ON p.input_id=v.input_id "
            "WHERE p.window_id=? ORDER BY v.input_id"
        ),
        "source_births": (
            "SELECT v.id,v.task_id,v.origin_result_id,v.initial_due_at,v.created_at "
            "FROM work_revisits v JOIN work_revisit_claims c ON c.revisit_id=v.id "
            "WHERE c.window_id=? ORDER BY v.id"
        ),
    }
    facts["choices"] = {
        name: rows(connection, query, (window_id,)) for name, query in queries.items()
    }
    facts["choices"]["frozen"] = saved is not None
    return facts

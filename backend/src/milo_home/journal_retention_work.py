"""Historical work facts required before moving a successful stream off the hot journal."""

import json

from .journal_retention_facts import one, require, rows
from .work_business_facts import with_choice_context
from .work_choice_evidence import require_revisit_input, validate_choices
from .work_deliverables import deliverable_path
from .work_receipts import validated_receipts
from .work_recovery import _result_row


def read_business(connection, snapshot):
    mapping = snapshot["work"]
    task_id, window_id = mapping["task_id"], mapping["window_id"]
    result = {
        "window": one(connection, "SELECT * FROM execution_windows WHERE id=?", (window_id,)),
        "task": one(connection, "SELECT * FROM work_tasks WHERE id=?", (task_id,)),
        "start": one(
            connection, "SELECT * FROM execution_start_intents WHERE window_id=?", (window_id,)
        ),
        "permits": rows(
            connection,
            "SELECT * FROM execution_permits WHERE window_id=? ORDER BY id",
            (window_id,),
        ),
        "inputs": rows(
            connection, "SELECT * FROM work_inputs WHERE task_id=? ORDER BY sequence", (task_id,)
        ),
        "results": rows(
            connection,
            "SELECT * FROM work_results WHERE window_id=? ORDER BY input_id",
            (window_id,),
        ),
        "steer_turns": rows(
            connection,
            "SELECT * FROM work_steer_turns WHERE task_id=? ORDER BY permit_id",
            (task_id,),
        ),
        "steer_inputs": rows(
            connection,
            "SELECT s.* FROM work_steer_inputs s JOIN work_inputs i ON i.id=s.input_id "
            "WHERE i.task_id=? ORDER BY s.input_id",
            (task_id,),
        ),
        "reconciliation": one(
            connection, "SELECT * FROM work_reconciliations WHERE window_id=?", (window_id,)
        ),
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
    return with_choice_context(connection, snapshot, result)


def _binding(snapshot, facts, linked):
    mapping = snapshot["work"]
    window, task, start = facts["window"], facts["task"], facts["start"]
    require(
        window is not None and task is not None and start is not None,
        "work_business_evidence_missing",
    )
    expected_mapping = {
        "stream": snapshot["stream"]["id"],
        "window_id": window["id"],
        "task_id": task["id"],
    }
    require(
        mapping == expected_mapping
        and window["task_id"] == task["id"]
        and window["unit_name"] == snapshot["stream"]["unit"]
        and start["window_id"] == window["id"]
        and start["stream_id"] == snapshot["stream"]["id"]
        and start["binding_id"] == linked.binding_id
        and (task["session_id"], task["owner_id"], task["workspace_id"])
        == (linked.session_id, linked.owner_id, linked.workspace_id),
        "work_binding_conflict",
    )
    require(
        window["state"] == "closed" and window["ended_at"] is not None, "work_window_not_closed"
    )
    require(task["status"] == "completed", "work_not_successfully_completed")
    require(
        all(
            p["state"] in {"finished", "cancelled"} and p["ended_at"] is not None
            for p in facts["permits"]
        ),
        "work_permits_unsettled",
    )
    require(
        all(i["delivery"] in {"acknowledged", "cancelled", "conflict"} for i in facts["inputs"])
        and all(turn["accepting"] == 0 for turn in facts["steer_turns"]),
        "work_inputs_unsettled",
    )


def _receipts(snapshot, facts, thread_id, starts):
    window, task = facts["window"], facts["task"]
    receipts = validated_receipts(
        snapshot["entries"],
        stream_id=snapshot["stream"]["id"],
        window_id=window["id"],
        task_id=task["id"],
        thread_id=thread_id,
    )
    launched = [p for p in facts["permits"] if p["launched_at"] is not None]
    require(
        len(receipts) == len(launched) == len(starts) > 0
        and {r["request_id"] for _, r in receipts} == set(starts)
        and {r["work"]["permit_id"] for _, r in receipts} == {p["id"] for p in launched},
        "work_receipts_incomplete",
    )
    for _, receipt in receipts:
        permit = next(p for p in launched if p["id"] == receipt["work"]["permit_id"])
        require(
            permit["state"] == "finished"
            and permit["input_id"] == receipt["work"]["input_id"]
            and permit["window_id"] == window["id"]
            and window["created_at"]
            <= permit["created_at"]
            <= permit["launched_at"]
            <= receipt["observed_at"]
            <= permit["ended_at"]
            <= window["ended_at"]
            and receipt["outcome"] == "completed",
            "work_receipt_permit_conflict",
        )
    expected = sorted((_result_row(window, s, r) for s, r in receipts), key=lambda r: r["input_id"])
    require(facts["results"] == expected, "work_result_projection_incomplete")
    return receipts


def _inputs(snapshot, facts, linked, receipts, thread_id):
    inputs = {row["id"]: row for row in facts["inputs"]}
    starts = {r["request_id"]: r for _, r in receipts}
    delivered = set()
    responses = {
        e["id"]: e["response"] for _, e in snapshot["entries"] if e.get("kind") == "response"
    }
    steers = {row["input_id"]: row["permit_id"] for row in facts["steer_inputs"]}
    turns = {r["work"]["permit_id"]: r["turn_id"] for _, r in receipts}
    for _, event in snapshot["entries"]:
        if event.get("kind") != "intent":
            continue
        request = event["request"]
        method, params = request["method"], request.get("params", {})
        if method == "turn/start":
            receipt = starts[request["id"]]
            saved = inputs[receipt["work"]["input_id"]]
            text_input = params.get("input", [])
            require(
                len(text_input) == 1 and text_input[0].get("type") == "text",
                "work_input_protocol_conflict",
            )
            payload = json.loads(text_input[0]["text"].rsplit("\n", 1)[-1])
            require_revisit_input(payload, saved, facts)
            require(
                payload["actor"]["id"] == linked.owner_id
                and all(
                    payload["task"][key] == facts["task"][key]
                    for key in ("id", "session_id", "workspace_id")
                )
                and all(
                    payload["input"][key] == saved[key]
                    for key in ("id", "sequence", "kind", "created_at", "body")
                )
                and payload["window"]["id"] == facts["window"]["id"]
                and payload["window"]["deadline_at"] == facts["window"]["deadline_at"]
                and saved["delivery"] == "acknowledged",
                "work_input_protocol_conflict",
            )
            delivered.add(saved["id"])
        elif method == "turn/steer":
            scope = event.get("work_steer", {})
            saved = inputs[scope["input_id"]]
            expected_scope = {
                "input_id": saved["id"],
                "permit_id": steers.get(saved["id"]),
                "window_id": facts["window"]["id"],
                "task_id": facts["task"]["id"],
                "stream_id": snapshot["stream"]["id"],
            }
            expected_params = {
                "threadId": thread_id,
                "expectedTurnId": turns[scope["permit_id"]],
                "clientUserMessageId": saved["id"],
                "input": [{"type": "text", "text": saved["body"]}],
            }
            rejected = "error" in responses[request["id"]]
            require(
                scope == expected_scope
                and params == expected_params
                and saved["delivery"] == ("conflict" if rejected else "acknowledged")
                and (
                    rejected
                    or responses[request["id"]].get("result", {}).get("turnId")
                    == turns[scope["permit_id"]]
                ),
                "work_steer_protocol_conflict",
            )
            require(saved["id"] not in delivered, "work_input_repeated")
            delivered.add(saved["id"])
    require(
        {i["id"] for i in facts["inputs"] if i["delivery"] in {"acknowledged", "conflict"}}
        == delivered,
        "work_input_delivery_incomplete",
    )


def _delivery(facts):
    deliverable, batch = facts["deliverable"], facts["attachment_batch"]
    require(
        deliverable is None or deliverable["observation"] in {"saved", "missing", "withheld"},
        "work_deliverable_pending",
    )
    require(
        batch is None or batch["state"] in {"none", "available", "unavailable"},
        "work_attachments_pending",
    )
    if batch is not None:
        require(
            (batch["state"] == "available") == bool(facts["attachments"]),
            "work_attachments_conflict",
        )
    if deliverable is not None and deliverable["observation"] == "saved":
        path = deliverable_path(facts["task"]["id"], deliverable["filename"])
        require(
            any(row["path"] == path for row in facts["artifacts"]),
            "work_deliverable_projection_missing",
        )


def validate(runtime, snapshot, facts, linked, starts, policy, **_):
    _binding(snapshot, facts, linked)
    thread_id = runtime.journal.binding().thread_id
    receipts = _receipts(snapshot, facts, thread_id, starts)
    decision = validate_choices(facts, receipts)
    _inputs(snapshot, facts, linked, receipts, thread_id)
    if decision in {None, "accepted"}:
        _delivery(facts)
    reconciliation = facts["reconciliation"]
    if reconciliation is not None:
        proof = dict(snapshot["entries"]).get(reconciliation["stop_sequence"], {})
        require(
            reconciliation["stream_id"] == snapshot["stream"]["id"]
            and reconciliation["outcome"] == "completed"
            and proof.get("kind") == "work_stop"
            and proof.get("checked_at") == reconciliation["checked_at"],
            "work_reconciliation_conflict",
        )
    return {
        "kind": "work",
        "window_id": facts["window"]["id"],
        "task_id": facts["task"]["id"],
        "completed_turns": len(receipts),
    }

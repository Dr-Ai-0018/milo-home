"""Historical work decisions are checked against their immutable receipt and launch basis."""

import json

from .journal_retention_facts import require


def validate_choices(facts, receipts):
    context = facts.get("choices")
    if context is None:
        return None
    require(context["frozen"], "work_choice_window_evidence_missing")
    original = {row["result_id"]: row for row in context["receipts"]}
    decisions = {row["result_id"]: row for row in context["decisions"]}
    bases = {row["permit_id"]: row for row in context["bases"]}
    receptions = {row["result_id"]: row for row in context["receptions"]}
    results = {row["input_id"]: row for row in facts["results"]}
    final = None
    for _, receipt in receipts:
        request = receipt.get("decision")
        result = results[receipt["work"]["input_id"]]
        saved = original.get(result["id"])
        decision = decisions.get(result["id"])
        require(
            request is not None
            and saved is not None
            and decision is not None
            and saved["applied"] == 1
            and json.loads(saved["payload"]) == request
            and result["permit_id"] in bases,
            "work_choice_projection_incomplete",
        )
        require(
            decision["state"] == request["decision"]
            and decision["receipt"] == request["receipt"]
            and decision["decided_at"] == receipt["observed_at"],
            "work_choice_projection_conflict",
        )
        revisit = [row for row in context["revisits"] if row["origin_result_id"] == result["id"]]
        if request["decision"] == "deferred":
            require(
                len(revisit) == 1
                and revisit[0]["id"] == decision["revisit_id"]
                and revisit[0]["initial_due_at"] == request["revisit_at"]
                and revisit[0]["created_at"] == receipt["observed_at"],
                "work_revisit_birth_conflict",
            )
        else:
            require(not revisit and decision["revisit_id"] is None, "work_revisit_birth_conflict")
        reception = receptions.get(result["id"])
        if request["reception"] is None:
            require(reception is None, "work_reception_birth_conflict")
        else:
            require(
                reception is not None
                and all(reception[key] == value for key, value in request["reception"].items())
                and reception["updated_at"] == receipt["observed_at"]
                and reception["owner_id"] == facts["task"]["owner_id"],
                "work_reception_birth_conflict",
            )
        final = request["decision"]
    require(
        len(original) == len(decisions) == len(bases) == len(receipts),
        "work_choice_projection_incomplete",
    )
    return final


def require_revisit_input(payload, saved, facts):
    context = facts.get("choices", {})
    claims = [row for row in context.get("claims", []) if row["input_id"] == saved["id"]]
    internal = [row for row in context.get("internal_inputs", []) if row["input_id"] == saved["id"]]
    if not claims:
        require(
            payload.get("source") == "milo_home.work_input.v1" and "revisit" not in payload,
            "work_input_protocol_conflict",
        )
        return
    require(len(claims) == 1, "work_revisit_claim_conflict")
    claim = claims[0]
    source = payload.get("revisit", {})
    births = [row for row in context.get("source_births", []) if row["id"] == claim["revisit_id"]]
    require(
        len(births) == 1
        and source.get("origin_result_id") == births[0]["origin_result_id"]
        and births[0]["task_id"] == facts["task"]["id"]
        and source.get("revisit_id") == claim["revisit_id"]
        and source.get("revision") == claim["revision"]
        and source.get("due_at") == claim["due_at"]
        and source.get("decision_revision") == claim["decision_revision"]
        and bool(source.get("internal")) == bool(internal)
        and claim["window_id"] == facts["window"]["id"],
        "work_revisit_claim_conflict",
    )
    if internal:
        require(
            len(internal) == 1
            and internal[0]["revisit_id"] == claim["revisit_id"]
            and internal[0]["revision"] == claim["revision"]
            and internal[0]["due_at"] == claim["due_at"],
            "work_revisit_claim_conflict",
        )
    expected = "milo_home.work_revisit.v1" if internal else "milo_home.work_input.v1"
    require(payload.get("source") == expected, "work_revisit_claim_conflict")

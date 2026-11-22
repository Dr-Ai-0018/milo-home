"""Shared fixtures must be coherent API packets, not independent UI inventions."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest
from conftest import activate, own_thread
from pydantic import ValidationError
from test_conversations import send

from milo_home.activity import ActivityView
from milo_home.conversation_models import ConversationMessagePage, ConversationView
from milo_home.interaction_models import MessageDecisionView
from milo_home.interaction_timing import ReceptionRequest, ReceptionView, RevisitSummary
from milo_home.reply_choice_result import ReplyChoiceResult
from milo_home.work_decision_result import WorkDecisionResult
from milo_home.work_models import WorkReceiptView, WorkSessionView, WorkTaskView

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = json.loads((ROOT / "examples/noncompulsory-interaction.json").read_text())
NOW = FIXTURES["observed_at"]
IDS = [str(UUID(int=number)) for number in (10, 11, 12)]


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda case: case["name"])
def test_shared_scene_packets_and_relations(case):
    from pydantic import TypeAdapter

    assert FIXTURES["synthetic"] is True
    responses = case["responses"]
    paths = FIXTURES["paths"]
    activity = ActivityView.model_validate(responses[paths["activity"]])
    messages = ConversationMessagePage.model_validate(responses[paths["messages"]]).messages
    conversation = ConversationView.model_validate(responses[paths["conversations"]][0])
    task = WorkTaskView.model_validate(responses[paths["task"]])
    WorkSessionView.model_validate(responses[paths["session"]])
    receipt = TypeAdapter(WorkReceiptView).validate_python(responses[paths["receipt"]])
    by_id = {message.id: message for message in messages}
    incoming = [message for message in messages if message.role == "visitor"]
    if activity.reply_summary is not None:
        assert activity.reply_summary == conversation.reply_summary
        assert sum(activity.reply_summary.model_dump().values()) == len(incoming)
        assert conversation.pending_count == (
            activity.reply_summary.unprocessed_count
            + activity.reply_summary.deferred_count
            + activity.reply_summary.unknown_count
        )
    for message in incoming:
        choice = message.reply_decision
        if choice is not None and choice.state == "replied":
            reply = by_id[choice.reply_id]
            assert reply.role == "milo" and message.id in reply.reply_to_ids
    if task.decision is not None:
        assert receipt.state == "available" and receipt.text == task.decision.receipt


def result(decisions, **changes):
    return ReplyChoiceResult.model_validate(
        {"version": 2, "decisions": decisions, "revisits": [], "reception": None, **changes}
    )


def test_mixed_choices_and_merged_scope_need_no_per_message_body():
    mixed = result(
        [
            {"kind": "reply", "message_ids": IDS[:2], "body": " 合着说一句。 "},
            {"kind": "no_reply", "message_ids": IDS[2:]},
        ]
    )
    mixed.require_scope(IDS, now=NOW)
    assert mixed.decisions[0].body == "合着说一句。"
    silent = result([{"kind": "no_reply", "message_ids": IDS}])
    silent.require_scope(IDS, now=NOW)
    deferred = result([{"kind": "defer", "message_ids": IDS, "revisit_at": NOW + 3600}])
    deferred.require_scope(IDS, now=NOW)


@pytest.mark.parametrize(
    "decisions",
    [
        [],
        [{"kind": "no_reply", "message_ids": IDS[:1]}],
        [{"kind": "no_reply", "message_ids": IDS + [str(UUID(int=99))]}],
        [
            {"kind": "no_reply", "message_ids": IDS},
            {"kind": "reply", "message_ids": IDS[:1], "body": "重复处置。"},
        ],
        [{"kind": "defer", "message_ids": IDS, "revisit_at": NOW}],
    ],
)
def test_missing_duplicate_foreign_or_past_decisions_rejected(decisions):
    with pytest.raises(ValueError):
        result(decisions).require_scope(IDS, now=NOW)


@pytest.mark.parametrize(
    "choice",
    [
        {"kind": "no_reply", "message_ids": IDS, "body": "假回信"},
        {"kind": "reply", "message_ids": IDS, "body": "   "},
        {"kind": "no_reply", "message_ids": [IDS[0], IDS[0]]},
        {"kind": "defer", "message_ids": IDS},
    ],
)
def test_invalid_choice_payloads_rejected(choice):
    with pytest.raises(ValidationError):
        result([choice])


def test_revisit_without_new_input_uses_only_frozen_sources():
    silent = result([])
    silent.require_scope([], revisit_source_ids=IDS, now=NOW)
    with pytest.raises(ValueError):
        silent.require_scope([], now=NOW)
    follow_up = result([{"kind": "reply", "message_ids": IDS, "body": "回来补一句。"}])
    follow_up.require_scope([], revisit_source_ids=IDS, now=NOW)
    with pytest.raises(ValueError):
        follow_up.require_scope([], revisit_source_ids=IDS[:1], now=NOW)


def test_competing_revisit_requests_and_stale_reception_rejected():
    deferred = {"kind": "defer", "message_ids": IDS, "revisit_at": NOW + 3600}
    with pytest.raises(ValueError):
        result(
            [deferred], revisits=[{"message_ids": IDS[:1], "revisit_at": NOW + 7200}]
        ).require_scope(IDS, now=NOW)
    with pytest.raises(ValueError):
        result(
            [{"kind": "no_reply", "message_ids": IDS}],
            reception={"scope": "reply", "mode": "later", "resume_on": "at_time", "until": NOW},
        ).require_scope(IDS, now=NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "available", "resume_on": "at_time", "until": NOW + 1},
        {"mode": "later", "resume_on": None},
        {"resume_on": "at_time", "until": None},
        {"resume_on": "new_input", "until": NOW + 1},
    ],
)
def test_reception_always_has_one_consistent_resume_condition(changes):
    with pytest.raises(ValidationError):
        ReceptionRequest.model_validate(
            {"scope": "all", "mode": "resting", "resume_on": "explicit", "until": None, **changes}
        )


def test_evidence_cannot_outlive_resume_and_categories_do_not_overlap():
    reception = deepcopy(
        FIXTURES["cases"][0]["responses"][FIXTURES["paths"]["activity"]]["reception"]
    )
    reception.update(mode="resting", resume_on="at_time", until=NOW + 1)
    with pytest.raises(ValidationError):
        ReceptionView.model_validate(reception)
    with pytest.raises(ValidationError):
        RevisitSummary.model_validate(
            {
                "pending_count": 1,
                "ready_count": 1,
                "active_count": 1,
                "blocked_count": 0,
                "reconciliation_count": 0,
                "next_due_at": NOW,
            }
        )
    with pytest.raises(ValidationError):
        MessageDecisionView.model_validate(
            {
                "state": "deferred",
                "revision": 1,
                "decided_at": NOW,
                "reply_id": None,
                "revisit": None,
            }
        )


@pytest.mark.parametrize("decision", ["accepted", "deferred", "declined", "needs_clarification"])
def test_work_decisions_are_not_execution_outcomes(decision):
    payload = {
        "version": 1,
        "decision": decision,
        "receipt": "  留一份简短回执。  ",
        "revisit_at": NOW + 3600 if decision == "deferred" else None,
        "reception": None,
    }
    output = WorkDecisionResult.model_validate(payload)
    output.require_time(NOW)
    assert output.receipt == "留一份简短回执。"
    if decision == "deferred":
        with pytest.raises(ValueError):
            output.require_time(NOW + 3600)
    else:
        with pytest.raises(ValidationError):
            WorkDecisionResult.model_validate({**payload, "revisit_at": NOW + 1})


def test_result_schemas_are_generated_from_the_actual_models():
    for filename, model in [
        ("reply-choice-result.schema.json", ReplyChoiceResult),
        ("work-decision-result.schema.json", WorkDecisionResult),
    ]:
        assert json.loads((ROOT / "contracts" / filename).read_text()) == model.model_json_schema()


def test_current_real_http_records_intake_without_fabricating_a_read_decision(client):
    activate(client)
    thread = own_thread(client)
    conversation = client.put(f"/api/threads/{thread}/chat").json()
    assert sum(conversation["reply_summary"].values()) == 0
    posted = send(client, conversation["id"], "契约检查，不触发执行。")
    assert posted.status_code == 201
    assert posted.json()["reply_decision"]["state"] == "unprocessed"
    assert posted.json()["reply_decision"]["decided_at"] is None
    assert posted.json()["reply_to_ids"] is None
    activity = client.get(f"/api/conversations/{conversation['id']}/activity").json()
    assert activity["reception"] is None
    assert activity["reply_summary"]["unprocessed_count"] == 1
    assert activity["revisits"]["pending_count"] == 0
    assert activity["reply_queue"]["pending_count"] == 1

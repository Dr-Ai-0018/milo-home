import json

import pytest
from pydantic import TypeAdapter, ValidationError

from milo_home.codex_config import NewawzConfig
from milo_home.codex_rpc import ProtocolError
from milo_home.work_models import WorkEventPage
from milo_home.work_output import OutputPolicy, extract_output
from milo_home.work_output_models import WorkOutputView
from milo_home.work_receipts import receipt_shape


def item(identity="answer", type_="agentMessage", **fields):
    return {"id": identity, "type": type_, **fields}


def notification(value, thread="main", turn="turn", method="item/completed"):
    return {
        "kind": "notification",
        "message": {
            "method": method,
            "params": {"threadId": thread, "turnId": turn, "item": value},
        },
    }


def extract(*events):
    return extract_output(
        list(enumerate(events)),
        thread_id="main",
        turn_id="turn",
        policy=OutputPolicy(("SYNTHETIC_CREDENTIAL",)),
    )


def test_only_exact_turn_final_items_are_projected_and_duplicates_count_once():
    tool = notification(
        item("tool", "commandExecution", command="PRIVATE", aggregatedOutput="SECRET")
    )
    final = notification(item(phase="final_answer", text="整理好了。"))
    result = extract(
        notification(item(phase="final_answer", text="wrong thread"), thread="other"),
        notification(item(phase="final_answer", text="wrong turn"), turn="other"),
        notification(item("reason", "reasoning", content=["PRIVATE"])),
        notification(item("comment", phase="commentary", text="PRIVATE")),
        notification(item("partial", phase="partial_answer", text="PRIVATE")),
        notification(item("delta", text="PRIVATE"), method="item/agentMessage/delta"),
        tool,
        tool,
        notification(item("patch", "fileChange", changes=["PRIVATE"])),
        final,
        final,
    )
    assert result == {
        "answer": {"state": "available", "body": "整理好了。", "reason": None},
        "actions": {"command_calls": 1, "file_change_calls": 1},
    }


@pytest.mark.parametrize(
    "text",
    [
        "SYNTHETIC_CREDENTIAL",
        "ＳＹＮＴＨＥＴＩＣ＿ＣＲＥＤＥＮＴＩＡＬ",
        "api_key = synthetic",
        "Authorization: Bearer synthetic",
        "password: synthetic",
        "https://synthetic.invalid/a?access_token=synthetic",
        "file:///root/private",
        "看 /etc/shadow",
        "看 /var/lib/private",
        "https://user:password@synthetic.invalid",
        "-----BEGIN RSA PRIVATE KEY-----",
        "abc\x1b[0m",
        "abc\u202e",
        "abc\u200b",
    ],
)
def test_sensitive_answer_withholds_whole_body(text):
    result = extract(notification(item(phase="final_answer", text=text)))
    assert result["answer"] == {"state": "withheld", "body": None, "reason": "sensitive"}
    assert text not in json.dumps(result)


@pytest.mark.parametrize(
    "events,reason",
    [
        ([], "no_final_answer"),
        ([notification(item(text="unknown phase"))], "no_final_answer"),
        ([notification(item(phase="final_answer", text="   "))], "no_final_answer"),
        ([notification(item(phase="final_answer", text="x" * 8001))], "too_long"),
        ([notification(item(phase="final_answer", text=42))], "ambiguous"),
        (
            [
                notification(item(phase="final_answer", text="first")),
                notification(item("another", phase="final_answer", text="second")),
            ],
            "ambiguous",
        ),
        (
            [
                notification(item(phase="final_answer", text="first")),
                notification(item(phase="final_answer", text="changed")),
            ],
            "ambiguous",
        ),
    ],
)
def test_missing_oversize_and_conflicting_output_have_explicit_reasons(events, reason):
    result = extract(*events)
    assert result["answer"]["body"] is None and result["answer"]["reason"] == reason


def test_runtime_policy_covers_live_credential_private_home_and_static_headers(tmp_path):
    config = NewawzConfig(
        "p",
        "m",
        "KEY",
        b"""[model_providers.p]
base_url="https://private.invalid"
[model_providers.p.http_headers]
X-Key="header-value"
""",
    )
    policy = OutputPolicy.for_runtime(config, "live-value", tmp_path)
    for value in ["header-value", "live-value", str(tmp_path), "https://private.invalid"]:
        assert not policy.permits("answer " + value)
        assert value not in repr(policy)
    assert policy.permits("已更新 src/example.py，检查还有一项没有通过。")


def test_counter_overflow_is_unknown_without_losing_the_answer():
    result = extract(
        *(notification(item(str(i), "commandExecution")) for i in range(10001)),
        notification(item(phase="final_answer", text="调用较多，这轮处理已结束。")),
    )
    assert result["actions"] is None
    assert result["answer"]["state"] == "available"


def test_answer_body_limits_and_discriminated_states_are_enforced():
    TypeAdapter(WorkOutputView).validate_python(
        extract(notification(item(phase="final_answer", text="x" * 8000)))
    )
    for answer in [
        {"state": "missing", "body": "not empty", "reason": "not_recorded"},
        {"state": "available", "body": None, "reason": None},
        {"state": "withheld", "body": None, "reason": "invented"},
    ]:
        with pytest.raises(ValidationError):
            WorkOutputView(answer=answer, actions=None)
    with pytest.raises(ValidationError):
        WorkEventPage(
            events=[
                {
                    "task_id": "10000000-0000-4000-8000-000000000001",
                    "sequence": 1,
                    "kind": "task_status",
                    "state": "acknowledged",
                    "input_id": None,
                    "result_id": None,
                    "recorded_at": 1,
                }
            ],
            next_after=1,
            has_more=False,
        )


def test_invalid_private_output_shape_does_not_pass_receipt_validation():
    from uuid import uuid4

    event = {
        "kind": "work_terminal",
        "work": {
            **{key: str(uuid4()) for key in ["input_id", "permit_id", "window_id", "task_id"]},
            "stream_id": "a" * 32,
        },
        "request_id": 1,
        "turn_id": "turn-1",
        "outcome": "completed",
        "observed_at": 1,
    }
    receipt_shape(event)
    with pytest.raises(ProtocolError):
        receipt_shape(event | {"output": {"raw": "private"}})

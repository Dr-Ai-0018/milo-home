"""Wire compatibility must not loosen saved-result or historical format checks."""

from copy import deepcopy

import pytest

from milo_home.reply_choice_result import ReplyChoiceResult
from milo_home.reply_results import (
    CHOICE_OUTPUT_SCHEMA,
    LEGACY_CHOICE_OUTPUT_SCHEMA,
    output_version,
    parse_result,
)


def test_strict_wire_union_is_equivalent_and_preserves_the_saved_contract():
    original = ReplyChoiceResult.model_json_schema()
    assert original == LEGACY_CHOICE_OUTPUT_SCHEMA
    wire = deepcopy(CHOICE_OUTPUT_SCHEMA)
    variants = wire["properties"]["decisions"]["items"]
    assert "oneOf" not in variants and "discriminator" not in variants
    assert variants.pop("anyOf") == original["properties"]["decisions"]["items"]["oneOf"]
    wire["properties"]["decisions"]["items"] = original["properties"]["decisions"]["items"]
    assert wire == original
    kinds = [
        original["$defs"][name]["properties"]["kind"]["const"]
        for name in ("ReplyChoice", "NoReplyChoice", "DeferChoice")
    ]
    assert len(set(kinds)) == 3
    assert output_version(CHOICE_OUTPUT_SCHEMA) == output_version(original) == 2
    wrong = deepcopy(CHOICE_OUTPUT_SCHEMA)
    wrong["properties"].pop("reception")
    with pytest.raises(ValueError, match="supported saved contract"):
        output_version(wrong)


def test_native_transport_change_does_not_allow_body_on_no_reply():
    with pytest.raises(ValueError):
        parse_result(
            '{"version":2,"decisions":[{"kind":"no_reply",'
            '"message_ids":["00000000-0000-0000-0000-000000000001"],'
            '"body":"fake"}],"revisits":[],"reception":null}',
            version=2,
        )

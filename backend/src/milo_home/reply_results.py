"""Decode sealed formats explicitly; old receipts retain their original bytes."""

import json
from copy import deepcopy

from .reply_choice_result import ReplyChoice, ReplyChoiceResult
from .reply_result import OUTPUT_SCHEMA, ReplyResult

LEGACY_CHOICE_OUTPUT_SCHEMA = ReplyChoiceResult.model_json_schema()


def _choice_output_schema():
    schema = deepcopy(LEGACY_CHOICE_OUTPUT_SCHEMA)
    variants = schema["properties"]["decisions"]["items"]
    # These branches have disjoint kind constants, so anyOf and oneOf accept
    # the same values. Native strict output rejects oneOf/discriminator.
    variants["anyOf"] = variants.pop("oneOf")
    variants.pop("discriminator")
    return schema


CHOICE_OUTPUT_SCHEMA = _choice_output_schema()


def parse_result(payload, *, version=None):
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Reply result must be an object")
    actual = data.get("version", 1)
    if version is not None and actual != version:
        raise ValueError("Reply result differs from its requested format")
    if actual == 2:
        return ReplyChoiceResult.model_validate(data)
    if actual == 1:
        return ReplyResult.model_validate(data)
    raise ValueError("Unsupported reply result format")


def require_scope(result, message_ids, completed_at, *, revisit_source_ids=()):
    if isinstance(result, ReplyChoiceResult):
        result.require_scope(message_ids, now=completed_at, revisit_source_ids=revisit_source_ids)
    else:
        if revisit_source_ids:
            raise ValueError("Legacy reply results cannot consume revisit sources")
        result.require_scope(message_ids)


def published_bodies(result):
    if isinstance(result, ReplyChoiceResult):
        return [item.body for item in result.decisions if isinstance(item, ReplyChoice)]
    return [reply.body for reply in result.replies]


def output_version(schema):
    if schema in (CHOICE_OUTPUT_SCHEMA, LEGACY_CHOICE_OUTPUT_SCHEMA):
        return 2
    if schema == OUTPUT_SCHEMA:
        return 1
    raise ValueError("Native reply output schema is not a supported saved contract")

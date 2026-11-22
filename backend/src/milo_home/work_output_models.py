"""Bounded plain-text answer and narrow, factual tool-call counters."""

from typing import Annotated, Literal

from pydantic import Field

from .schemas import InputModel

ANSWER_LIMIT = 8000


class AvailableAnswer(InputModel):
    state: Literal["available"]
    body: Annotated[str, Field(min_length=1, max_length=ANSWER_LIMIT)]
    reason: None


class MissingAnswer(InputModel):
    state: Literal["missing"]
    body: None
    reason: Literal["not_recorded", "no_final_answer"]


class WithheldAnswer(InputModel):
    state: Literal["withheld"]
    body: None
    reason: Literal["ambiguous", "too_long", "sensitive"]


class WorkActions(InputModel):
    command_calls: Annotated[int, Field(strict=True, ge=0, le=10000)]
    file_change_calls: Annotated[int, Field(strict=True, ge=0, le=10000)]


class WorkOutputView(InputModel):
    answer: Annotated[
        AvailableAnswer | MissingAnswer | WithheldAnswer, Field(discriminator="state")
    ]
    actions: WorkActions | None


def missing_output():
    return {
        "answer": {"state": "missing", "body": None, "reason": "not_recorded"},
        "actions": None,
    }

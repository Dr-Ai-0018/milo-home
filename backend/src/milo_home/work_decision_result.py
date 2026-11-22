"""Explicit work decisions, separate from the native turn's terminal outcome."""

from typing import Literal

from pydantic import Field, field_validator, model_validator

from .interaction_timing import Epoch, InteractionModel, ReceptionRequest


class WorkDecisionResult(InteractionModel):
    version: Literal[1]
    decision: Literal["accepted", "deferred", "declined", "needs_clarification"]
    receipt: str = Field(min_length=1, max_length=8000)
    revisit_at: Epoch | None
    reception: ReceptionRequest | None

    @field_validator("receipt")
    @classmethod
    def nonempty_receipt(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("A work decision requires a readable receipt")
        return value

    @model_validator(mode="after")
    def consistent_resume(self):
        if (self.decision == "deferred") != (self.revisit_at is not None):
            raise ValueError("Only deferred work schedules a resume revisit")
        return self

    def require_time(self, now: int):
        if self.revisit_at is not None and self.revisit_at <= now:
            raise ValueError("Work resume time must be in the future")
        if self.reception is not None and self.reception.until is not None:
            if self.reception.until <= now:
                raise ValueError("Reception resume time must be in the future")

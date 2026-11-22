"""Shared persisted timing shapes; these observations never assert presence."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Epoch = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Revision = Annotated[int, Field(strict=True, ge=1)]


class InteractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReceptionRequest(InteractionModel):
    scope: Literal["reply", "work", "all"]
    mode: Literal["available", "focusing", "resting", "later"]
    resume_on: Literal["at_time", "new_input", "explicit"] | None
    until: Epoch | None

    @model_validator(mode="after")
    def consistent_resume(self):
        if self.mode == "available":
            if self.resume_on is not None or self.until is not None:
                raise ValueError("Available reception has no pending resume condition")
        elif self.resume_on is None or (self.resume_on == "at_time") != (self.until is not None):
            raise ValueError("Reception requires one explicit, consistent resume condition")
        return self


class ReceptionView(ReceptionRequest):
    revision: Revision
    updated_at: Epoch
    observed_at: Epoch
    valid_until: Epoch

    @model_validator(mode="after")
    def consistent_observation(self):
        if not self.updated_at <= self.observed_at < self.valid_until:
            raise ValueError("Reception observation has inconsistent evidence times")
        if self.until is not None and self.valid_until > self.until:
            raise ValueError("Reception evidence must expire by its resume time")
        return self


class RevisitView(InteractionModel):
    id: UUID
    revision: Revision
    state: Literal[
        "scheduled", "running", "blocked", "needs_reconciliation", "completed", "cancelled"
    ]
    due_at: Epoch
    updated_at: Epoch
    blocked_reason: Literal["schedule", "reception", "budget", "service", "occupancy"] | None

    @model_validator(mode="after")
    def consistent_block(self):
        if (self.state == "blocked") != (self.blocked_reason is not None):
            raise ValueError("Only a blocked revisit has a known blocking reason")
        return self


class RevisitSummary(InteractionModel):
    pending_count: Annotated[int, Field(strict=True, ge=0)]
    ready_count: Annotated[int, Field(strict=True, ge=0)]
    active_count: Annotated[int, Field(strict=True, ge=0)]
    blocked_count: Annotated[int, Field(strict=True, ge=0)]
    reconciliation_count: Annotated[int, Field(strict=True, ge=0)]
    next_due_at: Epoch | None

    @model_validator(mode="after")
    def consistent_counts(self):
        classified = self.ready_count + self.active_count + self.blocked_count
        classified += self.reconciliation_count
        if classified > self.pending_count:
            raise ValueError("Revisit categories must be disjoint subsets of pending intents")
        if (self.pending_count == 0) != (self.next_due_at is None):
            raise ValueError("Pending revisits require an observed due time")
        return self

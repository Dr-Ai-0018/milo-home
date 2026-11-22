"""Read contracts for decisions, independently of execution and reply bodies."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .interaction_timing import Epoch, InteractionModel, Revision, RevisitView


class MessageDecisionView(InteractionModel):
    state: Literal["unprocessed", "replied", "no_reply", "deferred"]
    revision: Revision | None
    decided_at: Epoch | None
    reply_id: UUID | None
    revisit: RevisitView | None

    @model_validator(mode="after")
    def consistent_decision(self):
        if self.state == "unprocessed":
            if any(
                value is not None
                for value in (self.revision, self.decided_at, self.reply_id, self.revisit)
            ):
                raise ValueError("Unprocessed input has no decision evidence")
            return self
        if self.revision is None or self.decided_at is None:
            raise ValueError("A decision requires persisted revision and time evidence")
        if (self.state == "replied") != (self.reply_id is not None):
            raise ValueError("Only a replied decision identifies a published body")
        if self.state == "deferred":
            if self.revisit is None or self.revisit.state in {"completed", "cancelled"}:
                raise ValueError("Deferred input requires an unresolved revisit")
        return self


class ReplySummary(InteractionModel):
    unprocessed_count: Annotated[int, Field(strict=True, ge=0)]
    replied_count: Annotated[int, Field(strict=True, ge=0)]
    no_reply_count: Annotated[int, Field(strict=True, ge=0)]
    deferred_count: Annotated[int, Field(strict=True, ge=0)]
    unknown_count: Annotated[int, Field(strict=True, ge=0)]


class WorkDecisionView(InteractionModel):
    state: Literal["accepted", "deferred", "declined", "needs_clarification"]
    revision: Revision
    decided_at: Epoch
    receipt: str = Field(min_length=1, max_length=8000)
    revisit: RevisitView | None

    @model_validator(mode="after")
    def consistent_decision(self):
        if not self.receipt.strip():
            raise ValueError("A work decision requires a readable receipt")
        if self.state == "deferred":
            if self.revisit is None or self.revisit.state in {"completed", "cancelled"}:
                raise ValueError("Deferred work requires an unresolved revisit")
        elif self.revisit is not None:
            raise ValueError("Only deferred work carries a resume revisit")
        return self

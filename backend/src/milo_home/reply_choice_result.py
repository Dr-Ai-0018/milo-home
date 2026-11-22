"""Versioned native reply decisions and explicit follow-up requests."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator

from .interaction_timing import Epoch, InteractionModel, ReceptionRequest

MessageIds = Annotated[list[UUID], Field(min_length=1, max_length=10)]


class MessageChoice(InteractionModel):
    message_ids: MessageIds

    @field_validator("message_ids")
    @classmethod
    def distinct_messages(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Message identities must be unique")
        return value


class ReplyChoice(MessageChoice):
    kind: Literal["reply"]
    body: str = Field(min_length=1, max_length=4000)

    @field_validator("body")
    @classmethod
    def nonempty_body(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("A published reply requires a body")
        return value


class NoReplyChoice(MessageChoice):
    kind: Literal["no_reply"]


class DeferChoice(MessageChoice):
    kind: Literal["defer"]
    revisit_at: Epoch


class RevisitRequest(MessageChoice):
    revisit_at: Epoch


class ReplyChoiceResult(InteractionModel):
    version: Literal[2]
    decisions: Annotated[
        list[Annotated[ReplyChoice | NoReplyChoice | DeferChoice, Field(discriminator="kind")]],
        Field(max_length=10),
    ]
    revisits: list[RevisitRequest] = Field(max_length=10)
    reception: ReceptionRequest | None

    def require_scope(self, selected_ids, *, revisit_source_ids=(), now: int):
        selected = [UUID(str(value)) for value in selected_ids]
        sources = [UUID(str(value)) for value in revisit_source_ids]
        if len(selected) != len(set(selected)) or len(sources) != len(set(sources)):
            raise ValueError("Saved input identities must be unique")
        allowed = set(selected) | set(sources)
        if not allowed or len(allowed) > 10:
            raise ValueError("A turn needs a bounded, server-owned input or revisit source")
        covered = [value for choice in self.decisions for value in choice.message_ids]
        if len(covered) != len(set(covered)) or not set(selected) <= set(covered) <= allowed:
            raise ValueError("Decisions must cover new inputs once and stay in the frozen scope")
        requests = [choice for choice in self.decisions if isinstance(choice, DeferChoice)]
        requests.extend(self.revisits)
        scheduled = set()
        for request in requests:
            identities = set(request.message_ids)
            if not identities <= allowed or request.revisit_at <= now:
                raise ValueError("A revisit needs known sources and an explicit future time")
            if scheduled & identities:
                raise ValueError("One result cannot schedule competing revisits for the same input")
            scheduled.update(identities)
        if self.reception is not None and self.reception.until is not None:
            if self.reception.until <= now:
                raise ValueError("Reception resume time must be in the future")

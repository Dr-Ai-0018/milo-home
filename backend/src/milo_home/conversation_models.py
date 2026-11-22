from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .interaction_models import MessageDecisionView, ReplySummary
from .schemas import InputModel, MessagePage, MessageView


class LetterInput(InputModel):
    title: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("title")
    @classmethod
    def trim_title(cls, value):
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Empty title")
        return value


class ConversationView(InputModel):
    id: UUID
    thread_id: UUID
    kind: Literal["letter", "chat"]
    title: str | None
    is_default: bool
    created_at: int
    pending_count: int
    message_count: int
    last_message_at: int | None
    last_message_role: Literal["visitor", "milo"] | None
    last_reply_at: int | None
    reply_summary: ReplySummary | None = None


class ConversationMessageView(MessageView):
    conversation_id: UUID
    reply_decision: MessageDecisionView | None = None
    reply_to_ids: list[UUID] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def consistent_reply_relations(self):
        if self.role == "visitor" and self.reply_to_ids:
            raise ValueError("Incoming messages cannot claim reply coverage")
        if self.role == "milo" and self.reply_decision is not None:
            raise ValueError("Only incoming messages carry disposition evidence")
        if self.reply_to_ids is not None:
            if len(self.reply_to_ids) != len(set(self.reply_to_ids)):
                raise ValueError("Reply coverage identities must be distinct")
        return self


class ConversationMessagePage(MessagePage):
    messages: list[ConversationMessageView]
    next_before: int | None
    has_more_before: bool

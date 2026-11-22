"""Immutable native input identities, with separately identified revisit sources."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class NativeRevisitContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    revision: int = Field(strict=True, ge=1)
    due_at: int = Field(strict=True, ge=0)
    source_ids: tuple[UUID, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Revisit source identities must be unique")
        return self


class NativeMessageScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    owner_id: UUID
    conversation_id: UUID
    business_binding_id: UUID
    baseline_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_thread_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    message_ids: tuple[UUID, ...] = Field(max_length=10)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    revisit: NativeRevisitContext | None = None

    @model_serializer(mode="wrap")
    def compatible_encoding(self, handler):
        value = handler(self)
        if self.revisit is None:
            value.pop("revisit", None)
        return value

    @model_validator(mode="after")
    def unique_inputs(self):
        if len(set(self.message_ids)) != len(self.message_ids):
            raise ValueError("Native inputs must be unique")
        sources = self.revisit.source_ids if self.revisit else ()
        if not 1 <= len(self.message_ids) + len(sources) <= 10:
            raise ValueError("Native delivery needs bounded new inputs or a saved revisit")
        if set(self.message_ids) & set(sources):
            raise ValueError("Revisit sources cannot be delivered again as fresh input")
        return self

    @property
    def input_id(self):
        return "reply-" + self.run_id.hex

    def encoded(self):
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)

    @property
    def digest(self):
        return hashlib.sha256(self.encoded().encode()).hexdigest()


class NativeMessageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["sent", "accepted", "terminal", "stopped"]
    stream_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_id: int = Field(strict=True, ge=1)
    turn_id: str | None = Field(default=None, min_length=1, max_length=128)
    outcome: Literal["completed", "failed", "interrupted"] | None = None
    result_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stop_confirmed: bool | None = Field(default=None, strict=True)

    def encoded(self):
        return self.model_dump_json()

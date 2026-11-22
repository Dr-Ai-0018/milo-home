"""Explicit public content and the read-only website response boundary."""

from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PublicNoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=240)
    body: str = Field(min_length=1, max_length=12000)
    category: str | None = Field(default=None, min_length=1, max_length=40)
    content_date: date


class PublicNoteSummary(BaseModel):
    id: UUID
    sequence: int = Field(ge=1)
    revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=240)
    category: str | None
    content_date: date
    published_at: int = Field(ge=0)
    updated_at: int = Field(ge=0)


class PublicNoteDetail(PublicNoteSummary):
    body: str = Field(min_length=1, max_length=12000)


class PublicNotePage(BaseModel):
    notes: list[PublicNoteSummary]
    next_before: int | None = Field(ge=1)

"""Local work snapshots and the deliberately smaller anonymous read boundary."""

import unicodedata
from datetime import date
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PublicWorkLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=60)
    url: str = Field(max_length=2048)

    @field_validator("label")
    @classmethod
    def label_text(cls, value):
        if not value.strip():
            raise ValueError("A link needs a label")
        return value.strip()

    @field_validator("url")
    @classmethod
    def public_https(cls, value):
        if any(c.isspace() or unicodedata.category(c).startswith("C") for c in value):
            raise ValueError("Links cannot contain whitespace or controls")
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "\\" in value
        ):
            raise ValueError("Expected an absolute HTTPS link without credentials")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid HTTPS port")
        return value


class PublicWorkCoverInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    asset_id: UUID
    alt: str = Field(min_length=1, max_length=240)
    width: int = Field(gt=0, le=16000000, strict=True)
    height: int = Field(gt=0, le=16000000, strict=True)


class PublicWorkInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=240)
    body: str = Field(min_length=1, max_length=12000)
    category: str | None = Field(default=None, min_length=1, max_length=40)
    version_label: str | None = Field(default=None, min_length=1, max_length=60)
    content_date: date
    credits: str | None = Field(default=None, min_length=1, max_length=500)
    links: list[PublicWorkLink] = Field(default_factory=list, max_length=4)
    files: list[UUID] = Field(default_factory=list, max_length=16)
    cover: PublicWorkCoverInput | None = None

    @model_validator(mode="after")
    def bounded_assets(self):
        if len(set(self.files)) != len(self.files):
            raise ValueError("Duplicate download assets")
        selected = set(self.files)
        if self.cover:
            selected.add(self.cover.asset_id)
        if len(selected) > 16:
            raise ValueError("Cover and downloads share the sixteen asset limit")
        return self


class PublicWorkCover(PublicWorkCoverInput):
    url: str


class PublicWorkFile(BaseModel):
    id: UUID
    filename: str = Field(min_length=1, max_length=160)
    size_bytes: int = Field(ge=0, le=32 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    download_url: str


class PublicWorkSummary(BaseModel):
    id: UUID
    sequence: int = Field(ge=1)
    revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=240)
    category: str | None
    version_label: str | None
    content_date: date
    published_at: int = Field(ge=0)
    updated_at: int = Field(ge=0)
    cover: PublicWorkCover | None


class PublicWorkDetail(PublicWorkSummary):
    body: str = Field(min_length=1, max_length=12000)
    credits: str | None
    links: list[PublicWorkLink] = Field(max_length=4)
    files: list[PublicWorkFile] = Field(max_length=16)


class PublicWorkPage(BaseModel):
    works: list[PublicWorkSummary]
    next_before: int | None = Field(ge=1)

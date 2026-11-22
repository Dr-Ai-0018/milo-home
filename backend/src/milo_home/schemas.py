from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


class LoginInput(InputModel):
    handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,31}$")
    password: SecretStr = Field(min_length=12, max_length=128)


class ActivateInput(InputModel):
    token: SecretStr = Field(min_length=43, max_length=64)
    password: SecretStr = Field(min_length=12, max_length=128)
    display_name: str = Field(min_length=1, max_length=40)

    @field_validator("display_name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Empty display name")
        return value


class UserView(BaseModel):
    id: UUID
    handle: str
    display_name: str


class MessageInput(InputModel):
    body: str = Field(min_length=1, max_length=4000)

    @field_validator("body")
    @classmethod
    def trim_body(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Empty message")
        return value


class MessageView(BaseModel):
    id: UUID
    thread_id: UUID
    sequence: int
    role: Literal["visitor", "milo"]
    body: str
    in_reply_to: UUID | None
    created_at: int


class MessagePage(BaseModel):
    messages: list[MessageView]
    next_after: int


class ThreadView(BaseModel):
    id: UUID
    created_at: int
    pending_count: int


class RunView(BaseModel):
    id: UUID
    status: Literal[
        "queued", "running", "stopping", "succeeded", "failed", "cancelled", "timed_out"
    ]
    created_at: int
    started_at: int | None
    ended_at: int | None
    deadline_at: int | None
    failure_code: str | None

    @field_validator("failure_code")
    @classmethod
    def safe_failure_code(cls, value):
        allowed = {
            "execution_failed",
            "configuration_error",
            "invalid_result",
            "cancelled",
            "timed_out",
            "host_cpu_limit",
            "output_limit",
            "interrupted",
        }
        return value if value is None or value in allowed else "execution_failed"

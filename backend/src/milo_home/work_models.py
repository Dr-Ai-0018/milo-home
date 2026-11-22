from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator

from .interaction_models import WorkDecisionView
from .interaction_timing import ReceptionView
from .schemas import InputModel, MessageInput
from .work_deliverables import validate_filename
from .work_output_models import WorkOutputView


class EmptyWorkInput(InputModel):
    pass


class AppendWorkInput(MessageInput):
    active_turn: UUID | None = None


class CreateWorkInput(MessageInput):
    deliverable_name: str | None = Field(default=None, max_length=100)

    @field_validator("deliverable_name")
    @classmethod
    def valid_deliverable(cls, value):
        return None if value is None else validate_filename(value)


class WorkDeliverableView(InputModel):
    filename: str
    path: str
    state: Literal["pending", "saved", "missing", "withheld"]


class WorkspaceView(InputModel):
    id: UUID
    name: str


class WorkSessionView(InputModel):
    id: UUID
    workspace_id: UUID
    created_at: int
    execution_available: Literal[False] = False
    reception: ReceptionView | None = None


class WorkInputView(InputModel):
    id: UUID
    task_id: UUID
    sequence: int
    kind: Literal["initial", "supplement"]
    body: str
    delivery: Literal["queued", "sending", "acknowledged", "unknown", "cancelled", "conflict"]
    created_at: int
    active_turn: UUID | None = None
    source: Literal["visitor", "revisit"] | None = None


class WorkTaskView(InputModel):
    id: UUID
    session_id: UUID
    sequence: int
    status: Literal[
        "queued",
        "running",
        "stopping",
        "completed",
        "failed",
        "cancelled",
        "timed_out",
        "needs_reconciliation",
    ]
    created_at: int
    updated_at: int
    stop_requested_at: int | None
    initial_input: WorkInputView
    deliverable: WorkDeliverableView | None = None
    active_turn: UUID | None = None
    execution_available: Literal[False] = False
    decision: WorkDecisionView | None = None


class WorkTaskPage(InputModel):
    tasks: list[WorkTaskView]
    next_after: int


WorkTaskStatusGroup = Literal["all", "active", "needs_reconciliation", "finished"]


class WorkTaskHistoryPage(InputModel):
    tasks: list[WorkTaskView]
    next_before: int | None


class WorkInputPage(InputModel):
    inputs: list[WorkInputView]
    next_after: int


class WorkTurnResultView(InputModel):
    id: UUID
    task_id: UUID
    input_id: UUID
    outcome: Literal["completed", "failed", "interrupted"]
    observed_at: int
    output: WorkOutputView


class WorkResultList(InputModel):
    results: list[WorkTurnResultView]


class AvailableWorkReceipt(InputModel):
    state: Literal["available"]
    text: Annotated[str, Field(min_length=1, max_length=8200)]


class UnreadableWorkReceipt(InputModel):
    state: Literal["pending", "unavailable"]
    text: None


WorkReceiptView = Annotated[
    AvailableWorkReceipt | UnreadableWorkReceipt, Field(discriminator="state")
]


class WorkArtifactView(InputModel):
    path: str = Field(min_length=1, max_length=240)
    text: str = Field(max_length=16_384)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checked_at: int = Field(ge=0)


class WorkArtifactList(InputModel):
    artifacts: list[WorkArtifactView] = Field(max_length=8)


class WorkAttachmentView(InputModel):
    id: UUID
    filename: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkAttachmentList(InputModel):
    state: Literal["none", "pending", "available", "unavailable"]
    attachments: list[WorkAttachmentView] = Field(max_length=16)


class WorkAttachmentArchive(InputModel):
    attachment_ids: list[UUID] | None = Field(min_length=1, max_length=16)

    @field_validator("attachment_ids")
    @classmethod
    def distinct_ids(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Duplicate attachments")
        return value


class WorkEventBase(InputModel):
    task_id: UUID
    sequence: int
    recorded_at: int


class WorkTaskEvent(WorkEventBase):
    kind: Literal["task_status"]
    state: Literal[
        "queued",
        "running",
        "stopping",
        "completed",
        "failed",
        "cancelled",
        "timed_out",
        "needs_reconciliation",
    ]
    input_id: None
    result_id: None


class WorkInputEvent(WorkEventBase):
    kind: Literal["input_delivery"]
    state: Literal["queued", "sending", "acknowledged", "unknown", "cancelled", "conflict"]
    input_id: UUID
    result_id: None


class WorkResultEvent(WorkEventBase):
    kind: Literal["turn_result"]
    state: Literal["completed", "failed", "interrupted"]
    input_id: UUID
    result_id: UUID


WorkEventView = Annotated[
    WorkTaskEvent | WorkInputEvent | WorkResultEvent, Field(discriminator="kind")
]


class WorkEventPage(InputModel):
    events: list[WorkEventView]
    next_after: int
    has_more: bool

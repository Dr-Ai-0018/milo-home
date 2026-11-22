from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from .routes import User
from .work_artifacts import list_artifacts
from .work_models import (
    AppendWorkInput,
    CreateWorkInput,
    EmptyWorkInput,
    WorkArtifactList,
    WorkEventPage,
    WorkInputPage,
    WorkInputView,
    WorkReceiptView,
    WorkResultList,
    WorkSessionView,
    WorkspaceView,
    WorkTaskHistoryPage,
    WorkTaskPage,
    WorkTaskStatusGroup,
    WorkTaskView,
)
from .work_task_receipt import read_receipt

router = APIRouter(prefix="/api")
After = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=50)]
Key = Annotated[UUID, Header(alias="Idempotency-Key")]


@router.get("/workspaces", response_model=list[WorkspaceView])
def workspaces(request: Request, user: User):
    return request.app.state.work_access.workspaces(user["id"])


@router.put(
    "/workspaces/{workspace_id}/main-session",
    response_model=WorkSessionView,
    responses={201: {"model": WorkSessionView}},
)
def main_session(
    workspace_id: UUID, body: EmptyWorkInput, request: Request, response: Response, user: User
):
    session, created = request.app.state.work_tasks.main_session(user["id"], str(workspace_id))
    response.status_code = 201 if created else 200
    return session


@router.get("/work-sessions/{session_id}", response_model=WorkSessionView)
def session(session_id: UUID, request: Request, user: User):
    return request.app.state.work_tasks.session(user["id"], str(session_id))


@router.post(
    "/work-sessions/{session_id}/tasks",
    response_model=WorkTaskView,
    status_code=202,
    responses={200: {"model": WorkTaskView}},
)
def create_task(
    session_id: UUID,
    body: CreateWorkInput,
    request: Request,
    response: Response,
    user: User,
    key: Key,
):
    task, created = request.app.state.work_tasks.create(
        user["id"], str(session_id), body.body, str(key), body.deliverable_name
    )
    response.status_code = 202 if created else 200
    return task


@router.get("/work-sessions/{session_id}/tasks", response_model=WorkTaskPage)
def tasks(session_id: UUID, request: Request, user: User, after: After = 0, limit: Limit = 20):
    return request.app.state.work_tasks.list(user["id"], str(session_id), after, limit)


@router.get("/work-sessions/{session_id}/task-history", response_model=WorkTaskHistoryPage)
def task_history(
    session_id: UUID,
    request: Request,
    user: User,
    before: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    limit: Limit = 20,
    status_group: WorkTaskStatusGroup = "all",
):
    return request.app.state.work_tasks.history(
        user["id"], str(session_id), before, limit, status_group
    )


@router.get("/work-tasks/{task_id}", response_model=WorkTaskView)
def task(task_id: UUID, request: Request, user: User):
    return request.app.state.work_tasks.get(user["id"], str(task_id))


@router.post(
    "/work-tasks/{task_id}/inputs",
    response_model=WorkInputView,
    status_code=202,
    responses={200: {"model": WorkInputView}},
)
def append_input(
    task_id: UUID, body: AppendWorkInput, request: Request, response: Response, user: User, key: Key
):
    item, created = request.app.state.work_tasks.append(
        user["id"],
        str(task_id),
        body.body,
        str(key),
        str(body.active_turn) if body.active_turn is not None else None,
    )
    response.status_code = 202 if created else 200
    return item


@router.get("/work-tasks/{task_id}/inputs", response_model=WorkInputPage)
def inputs(task_id: UUID, request: Request, user: User, after: After = 0, limit: Limit = 20):
    return request.app.state.work_tasks.inputs(user["id"], str(task_id), after, limit)


@router.post("/work-tasks/{task_id}/stop", response_model=WorkTaskView, status_code=202)
def stop(task_id: UUID, body: EmptyWorkInput, request: Request, user: User, key: Key):
    return request.app.state.work_tasks.stop(user["id"], str(task_id), str(key))


@router.get("/work-tasks/{task_id}/results", response_model=WorkResultList)
def results(task_id: UUID, request: Request, user: User):
    return request.app.state.work_progress.results(user["id"], str(task_id))


@router.get("/work-tasks/{task_id}/receipt", response_model=WorkReceiptView)
def receipt(task_id: UUID, request: Request, user: User):
    return read_receipt(request.app.state.database, user["id"], str(task_id))


@router.get("/work-tasks/{task_id}/artifacts", response_model=WorkArtifactList)
def artifacts(task_id: UUID, request: Request, user: User):
    return list_artifacts(request.app.state.database, user["id"], str(task_id))


@router.get("/work-tasks/{task_id}/events", response_model=WorkEventPage)
def events(task_id: UUID, request: Request, user: User, after: After = 0, limit: Limit = 20):
    return request.app.state.work_progress.events(user["id"], str(task_id), after, limit)

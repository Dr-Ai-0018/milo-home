from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from .activity import ActivityView
from .conversation_models import (
    ConversationMessagePage,
    ConversationMessageView,
    ConversationView,
    LetterInput,
)
from .knocks import KnockInput, KnockView
from .routes import User
from .schemas import MessageInput

router = APIRouter(prefix="/api")


@router.get("/conversations/{conversation_id}/activity", response_model=ActivityView)
def activity(conversation_id: UUID, request: Request, user: User):
    return request.app.state.activity.view(user["id"], str(conversation_id))


@router.post("/conversations/{conversation_id}/knocks", response_model=KnockView, status_code=201)
def knock(
    conversation_id: UUID,
    body: KnockInput,
    request: Request,
    response: Response,
    user: User,
    idempotency_key: Annotated[UUID, Header()],
):
    receipt, created = request.app.state.knocks.receive(
        user["id"], str(conversation_id), str(idempotency_key)
    )
    response.status_code = 201 if created else 200
    return receipt


@router.get("/threads/{thread_id}/conversations", response_model=list[ConversationView])
def conversations(thread_id: UUID, request: Request, user: User):
    return request.app.state.conversations.list(user["id"], str(thread_id))


@router.post("/threads/{thread_id}/conversations", response_model=ConversationView, status_code=201)
def create_letter(
    thread_id: UUID,
    body: LetterInput,
    request: Request,
    response: Response,
    user: User,
    idempotency_key: Annotated[UUID, Header()],
):
    conversation, created = request.app.state.conversations.create_letter(
        user["id"], str(thread_id), body.title, str(idempotency_key)
    )
    response.status_code = 201 if created else 200
    return conversation


@router.put("/threads/{thread_id}/chat", response_model=ConversationView)
def chat(thread_id: UUID, request: Request, user: User):
    return request.app.state.conversations.chat(user["id"], str(thread_id))


@router.get("/conversations/{conversation_id}/messages", response_model=ConversationMessagePage)
def messages(
    conversation_id: UUID,
    request: Request,
    user: User,
    after: Annotated[int | None, Query(ge=0, le=9223372036854775807)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    latest: bool = False,
):
    return request.app.state.conversations.list_messages(
        user["id"], str(conversation_id), after, limit, before=before, latest=latest
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=ConversationMessageView,
    status_code=201,
)
def post_message(
    conversation_id: UUID,
    body: MessageInput,
    request: Request,
    response: Response,
    user: User,
    idempotency_key: Annotated[UUID, Header()],
):
    message, created = request.app.state.conversations.post(
        user["id"], str(conversation_id), body.body, str(idempotency_key)
    )
    response.status_code = 201 if created else 200
    return message

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, Security
from fastapi.security import APIKeyCookie

from .schedule_models import ScheduleView
from .schemas import (
    ActivateInput,
    LoginInput,
    MessageInput,
    MessagePage,
    MessageView,
    RunView,
    ThreadView,
    UserView,
)

COOKIE_NAME = "milo_session"
router = APIRouter(prefix="/api")
cookie_auth = APIKeyCookie(name=COOKIE_NAME, auto_error=False)
SessionToken = Annotated[str | None, Security(cookie_auth)]


def current_user(request: Request, token: SessionToken) -> dict:
    return request.app.state.auth.authenticate(token)


User = Annotated[dict, Depends(current_user)]


@router.get("/schedule", response_model=ScheduleView)
def schedule(request: Request):
    return request.app.state.schedules.view()


def set_session(request: Request, response: Response, token: str):
    settings = request.app.state.settings
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_seconds,
        httponly=True,
        secure=settings.secure_cookie,
        samesite="strict",
        path="/",
    )


def limit_auth(request: Request, action: str):
    client = request.client.host if request.client else "unknown"
    request.app.state.auth.limit_attempt(client, action)


@router.post("/auth/accept-invite", response_model=UserView, status_code=201)
def activate(body: ActivateInput, request: Request, response: Response):
    limit_auth(request, "activate")
    user, token = request.app.state.auth.activate(
        body.token.get_secret_value(), body.password.get_secret_value(), body.display_name
    )
    set_session(request, response, token)
    return user


@router.post("/auth/login", response_model=UserView)
def login(body: LoginInput, request: Request, response: Response):
    limit_auth(request, "login")
    user, token = request.app.state.auth.login(body.handle, body.password.get_secret_value())
    set_session(request, response, token)
    return user


@router.post("/auth/logout", status_code=204)
def logout(request: Request):
    request.app.state.auth.logout(request.cookies.get(COOKIE_NAME))
    response = Response(status_code=204)
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=request.app.state.settings.secure_cookie,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/auth/me", response_model=UserView)
def me(user: User):
    return user


@router.get("/threads", response_model=list[ThreadView])
def threads(request: Request, user: User):
    return request.app.state.messages.list_threads(user["id"])


@router.get("/threads/{thread_id}/runs", response_model=list[RunView])
def runs(thread_id: UUID, request: Request, user: User):
    return request.app.state.runs.list_owned(user["id"], str(thread_id))


@router.get("/threads/{thread_id}/messages", response_model=MessagePage)
def messages(
    thread_id: UUID,
    request: Request,
    user: User,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    return request.app.state.messages.list_messages(user["id"], str(thread_id), after, limit)


@router.post("/threads/{thread_id}/messages", response_model=MessageView, status_code=201)
def post_message(
    thread_id: UUID,
    body: MessageInput,
    request: Request,
    response: Response,
    user: User,
    idempotency_key: Annotated[UUID, Header()],
):
    message, created = request.app.state.messages.post(
        user["id"], str(thread_id), body.body, str(idempotency_key)
    )
    response.status_code = 201 if created else 200
    return message

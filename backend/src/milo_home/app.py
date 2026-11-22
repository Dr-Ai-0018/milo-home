import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .activity import ActivityService
from .auth import AuthService
from .config import Settings
from .conversation_routes import router as conversation_router
from .conversations import ConversationService
from .database import Database
from .errors import ApiError
from .http_security import HttpSecurity, error_response
from .knocks import KnockService
from .maintenance_routes import router as maintenance_router
from .messages import MessageService
from .public_asset_store import PublicAssetStore
from .public_notes import PublicNotes
from .public_notes_routes import router as public_notes_router
from .public_works import PublicWorks
from .public_works_routes import router as public_works_router
from .routes import router
from .runs import RunStore
from .schedules import ScheduleStore
from .schemas import ErrorEnvelope
from .work_access import WorkAccess
from .work_attachment_routes import router as attachment_router
from .work_attachment_store import AttachmentStore
from .work_progress import WorkProgress
from .work_routes import router as work_router
from .work_tasks import WorkTasks


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = settings or Settings.from_env()
    database = Database(configuration.database, paths=configuration.paths)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.initialize()
        app.state.auth = AuthService(database, configuration)
        app.state.messages = MessageService(database)
        app.state.conversations = ConversationService(database)
        app.state.activity = ActivityService(database)
        app.state.knocks = KnockService(database)
        app.state.runs = RunStore(database)
        app.state.schedules = ScheduleStore(database)
        app.state.public_notes = PublicNotes(database)
        app.state.public_works = PublicWorks(database, store=app.state.public_asset_store)
        app.state.work_access = WorkAccess(database)
        app.state.work_tasks = WorkTasks(database)
        app.state.work_progress = WorkProgress(database)
        yield

    app = FastAPI(
        title="米洛在家",
        version="0.1.0",
        lifespan=lifespan,
        responses={
            status: {"model": ErrorEnvelope}
            for status in [400, 401, 403, 404, 409, 413, 422, 429, 500, 503]
        },
    )
    app.state.settings = configuration
    app.state.database = database
    attachments = configuration.work_attachments
    app.state.work_attachment_store = (
        AttachmentStore(attachments.root, max_bytes=attachments.max_bytes) if attachments else None
    )
    assets = configuration.public_assets
    app.state.public_asset_store = (
        PublicAssetStore(assets.root, max_bytes=assets.max_bytes) if assets else None
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=configuration.hosts)
    app.add_middleware(HttpSecurity, settings=configuration)
    app.include_router(router)
    app.include_router(conversation_router)
    app.include_router(work_router)
    app.include_router(attachment_router)
    app.include_router(maintenance_router)
    app.include_router(public_notes_router)
    app.include_router(public_works_router)

    @app.get("/api/health")
    def health():
        with database.connect() as connection:
            connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        return {
            "status": "ok",
            "automatic_wakeup": False,
        }

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, error: RequestValidationError):
        # Pydantic errors can include the submitted password or invitation token.
        return error_response(422, "invalid_input", "请检查输入内容。")

    @app.exception_handler(ApiError)
    async def business_error(request: Request, error: ApiError):
        return error_response(error.status, error.code, error.message)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, error: sqlite3.Error):
        if getattr(error, "sqlite_errorcode", None) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return error_response(503, "database_busy", "收件服务繁忙，请稍后重试。")
        return error_response(500, "storage_error", "记录暂时无法读取或保存。")

    return app

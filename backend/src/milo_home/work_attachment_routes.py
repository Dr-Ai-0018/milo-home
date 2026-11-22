from tempfile import TemporaryFile
from threading import BoundedSemaphore
from urllib.parse import quote
from uuid import UUID
from zipfile import ZIP_STORED, ZipFile

from fastapi import APIRouter, Request, Response
from starlette.responses import StreamingResponse

from .errors import ApiError
from .routes import User
from .work_access import require_task
from .work_attachment_batches import (
    read_attachment,
    read_attachments,
    read_sealed,
    recheck_attachment_access,
    select_attachments,
)
from .work_models import WorkAttachmentArchive, WorkAttachmentList

router = APIRouter(prefix="/api/work-tasks/{task_id}/attachments")
DOWNLOAD_SLOTS = BoundedSemaphore(2)
CHUNK = 256 * 1024


def _download_headers(filename, size=None):
    headers = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe="")}
    if size is not None:
        headers["Content-Length"] = str(size)
    return headers


def _context(request, owner, task_id):
    database = request.app.state.database
    store = request.app.state.work_attachment_store
    with database.connect() as connection:
        require_task(connection, owner, task_id)
    return database, store


def _reserve(store):
    if store is None:
        raise ApiError(409, "work_attachment_unavailable", "附件下载尚未开放。")
    if not DOWNLOAD_SLOTS.acquire(blocking=False):
        raise ApiError(503, "work_attachment_busy", "附件下载繁忙，请稍后再试。")


def _chunks(file):
    while block := file.read(CHUNK):
        yield block


class _DownloadStream(StreamingResponse):
    def __init__(self, file, size):
        self.file = file
        super().__init__(
            _chunks(file),
            media_type="application/zip",
            headers=_download_headers("attachments.zip", size),
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            _release_archive(self.file)


class _SingleDownload(Response):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            DOWNLOAD_SLOTS.release()


@router.get("", response_model=WorkAttachmentList)
def attachments(task_id: UUID, request: Request, user: User):
    database, store = _context(request, user["id"], str(task_id))
    if store is None:
        return {"state": "none", "attachments": []}
    return read_attachments(database, user["id"], str(task_id))


@router.get(
    "/{attachment_id}/download",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
def download(task_id: UUID, attachment_id: UUID, request: Request, user: User):
    owner, task = user["id"], str(task_id)
    database, store = _context(request, owner, task)
    _reserve(store)
    try:
        item, data = read_attachment(database, owner, task, str(attachment_id), store=store)
        return _SingleDownload(
            data,
            media_type="application/octet-stream",
            headers=_download_headers(item["filename"]),
        )
    except BaseException:
        DOWNLOAD_SLOTS.release()
        raise


@router.post(
    "/archive",
    response_class=Response,
    responses={
        200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}
    },
)
def archive(task_id: UUID, body: WorkAttachmentArchive, request: Request, user: User):
    owner, task = user["id"], str(task_id)
    database, store = _context(request, owner, task)
    selected = None if body.attachment_ids is None else list(map(str, body.attachment_ids))
    _reserve(store)
    output = None
    try:
        items = select_attachments(database, owner, task, selected)
        output = TemporaryFile(mode="w+b")
        with ZipFile(output, "w", compression=ZIP_STORED) as archive_file:
            for item in items:
                archive_file.writestr(item["filename"], read_sealed(store, task, item))
        size = output.tell()
        output.seek(0)
        recheck_attachment_access(database, owner, task)
        return _DownloadStream(output, size)
    except OSError as error:
        _release_archive(output)
        raise ApiError(503, "work_attachment_busy", "暂时无法打包，请稍后再试。") from error
    except BaseException:
        _release_archive(output)
        raise


def _release_archive(file):
    try:
        if file is not None:
            file.close()
    finally:
        DOWNLOAD_SLOTS.release()

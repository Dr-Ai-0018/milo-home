"""Anonymous current work reads. Files are rechecked immediately before response start."""

from threading import BoundedSemaphore
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from .errors import ApiError
from .public_asset_store import png_dimensions
from .public_work_records import read_asset
from .public_works_models import PublicWorkDetail, PublicWorkPage

router = APIRouter(prefix="/api/works")
DOWNLOAD_SLOTS = BoundedSemaphore(2)


@router.get("", response_model=PublicWorkPage)
def works(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=30)] = 10,
    before: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
):
    return request.app.state.public_works.list_works(limit=limit, before=before)


@router.get("/{work_id}", response_model=PublicWorkDetail)
def work(work_id: UUID, request: Request):
    return request.app.state.public_works.get(str(work_id))


class _AssetResponse(Response):
    def __init__(self, data, service, item, role):
        self.service, self.item, self.role = service, item, role
        headers = {}
        if role == "download":
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(
                item["filename"], safe=""
            )
        super().__init__(
            data,
            media_type="image/png" if role == "cover" else "application/octet-stream",
            headers=headers,
        )

    async def __call__(self, scope, receive, send):
        try:
            await run_in_threadpool(
                self.service.public_asset, self.item["work_id"], self.item["id"], self.role
            )
            await super().__call__(scope, receive, send)
        finally:
            DOWNLOAD_SLOTS.release()


def _asset_response(request, work_id, asset_id, role):
    service = request.app.state.public_works
    item = service.public_asset(str(work_id), str(asset_id), role)
    if not DOWNLOAD_SLOTS.acquire(blocking=False):
        raise ApiError(503, "work_asset_busy", "公开文件读取繁忙，请稍后重试。")
    try:
        data = read_asset(service.store, str(work_id), item)
        if role == "cover":
            try:
                if png_dimensions(data) != (item["width"], item["height"]):
                    raise ValueError("Cover dimensions differ")
            except ValueError as error:
                raise ApiError(503, "work_asset_unavailable", "封面暂时无法读取。") from error
        return _AssetResponse(data, service, item, role)
    except BaseException:
        DOWNLOAD_SLOTS.release()
        raise


@router.get(
    "/{work_id}/assets/{asset_id}/download",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
def download(work_id: UUID, asset_id: UUID, request: Request):
    return _asset_response(request, work_id, asset_id, "download")


@router.get(
    "/{work_id}/assets/{asset_id}/preview",
    response_class=Response,
    responses={200: {"content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}},
)
def preview(work_id: UUID, asset_id: UUID, request: Request):
    return _asset_response(request, work_id, asset_id, "cover")

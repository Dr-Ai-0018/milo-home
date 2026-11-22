"""Owner-only cached facts; HTTP never performs maintenance or directory scans."""

import time

from fastapi import APIRouter, Request

from .errors import ApiError
from .maintenance_cache import read_summary
from .maintenance_models import MaintenanceView
from .routes import User

router = APIRouter(prefix="/api")


@router.get("/maintenance", response_model=MaintenanceView)
def maintenance(request: Request, user: User):
    settings = request.app.state.settings
    selected = settings.maintenance
    if selected is None:
        raise ApiError(404, "maintenance_unavailable", "资料维护记录尚未接入。")
    if str(selected.owner) != user["id"] or not request.app.state.work_access.workspaces(
        user["id"]
    ):
        raise ApiError(403, "maintenance_unavailable", "暂时不能查看这份维护记录。")
    try:
        return read_summary(selected, settings.database, now=int(time.time()))
    except (OSError, ValueError):
        raise ApiError(
            503, "maintenance_unavailable", "维护记录暂时无法核对，请稍后重读。"
        ) from None

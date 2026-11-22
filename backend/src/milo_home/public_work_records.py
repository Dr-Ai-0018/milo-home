"""Persistence of work revisions and their explicit asset selection."""

import json
from uuid import UUID

from .errors import ApiError
from .public_asset_store import MAX_REVISION_BYTES, png_dimensions
from .public_works_models import PublicWorkInput

CONTENT_FIELDS = (
    "title",
    "summary",
    "body",
    "category",
    "version_label",
    "content_date",
    "credits",
)
SUMMARY_COLUMNS = (
    "w.id,w.sequence,r.revision,r.title,r.summary,r.category,r.version_label,r.content_date,"
    "w.published_at,w.public_updated_at AS updated_at"
)


def work_entry(connection, work_id):
    row = connection.execute(
        "SELECT * FROM public_works WHERE id=?", (str(UUID(work_id)),)
    ).fetchone()
    if row is None:
        raise ApiError(404, "work_not_found", "这件作品当前不可读取。")
    return row


def require_version(row, expected_version):
    if row["version"] != expected_version:
        raise ApiError(409, "work_changed", "作品已发生变化，请重新核对当前版本。")


def selections(connection, work_id, revision):
    return connection.execute(
        "SELECT a.*,s.role,s.position,s.alt,s.width,s.height FROM public_work_revision_assets s "
        "JOIN public_work_assets a ON a.work_id=s.work_id AND a.id=s.asset_id "
        "WHERE s.work_id=? AND s.revision=? ORDER BY s.role,s.position",
        (work_id, revision),
    ).fetchall()


def saved_content(connection, work_id, revision):
    if revision is None:
        return None
    row = connection.execute(
        "SELECT * FROM public_work_revisions WHERE work_id=? AND revision=?", (work_id, revision)
    ).fetchone()
    if row is None:
        raise ValueError("Work revision is missing")
    content = {key: row[key] for key in CONTENT_FIELDS}
    content.update(links=json.loads(row["links_json"]), files=[], cover=None)
    for item in selections(connection, work_id, revision):
        if item["role"] == "download":
            content["files"].append(item["id"])
        else:
            content["cover"] = {
                "asset_id": item["id"],
                "alt": item["alt"],
                "width": item["width"],
                "height": item["height"],
            }
    return PublicWorkInput.model_validate(content).model_dump(mode="json")


def local_view(connection, row):
    return {
        **dict(row),
        "latest": saved_content(connection, row["id"], row["latest_revision"]),
        "published": saved_content(connection, row["id"], row["published_revision"]),
        "assets": [
            dict(item)
            for item in connection.execute(
                "SELECT * FROM public_work_assets WHERE work_id=? ORDER BY created_at,id",
                (row["id"],),
            )
        ],
    }


def selected_assets(connection, work_id, content):
    ids = set(content["files"])
    if content["cover"]:
        ids.add(content["cover"]["asset_id"])
    items = []
    for asset_id in sorted(ids):
        row = connection.execute(
            "SELECT * FROM public_work_assets WHERE work_id=? AND id=?", (work_id, asset_id)
        ).fetchone()
        if row is None:
            raise ApiError(409, "work_asset_missing", "所选文件尚未登记在这件作品中。")
        items.append(dict(row))
    if sum(item["size_bytes"] for item in items) > MAX_REVISION_BYTES:
        raise ValueError("Work revision exceeds the asset budget")
    return items


def append_revision(connection, work_id, revision, content, now):
    selected_assets(connection, work_id, content)
    connection.execute(
        "INSERT INTO public_work_revisions "
        "(work_id,revision,title,summary,body,category,version_label,content_date,credits,"
        "links_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            work_id,
            revision,
            *[content[k] for k in CONTENT_FIELDS],
            json.dumps(content["links"], ensure_ascii=False),
            now,
        ),
    )
    for position, asset_id in enumerate(content["files"]):
        connection.execute(
            "INSERT INTO public_work_revision_assets (work_id,revision,asset_id,role,position) "
            "VALUES (?,?,?,'download',?)",
            (work_id, revision, asset_id, position),
        )
    if cover := content["cover"]:
        connection.execute(
            "INSERT INTO public_work_revision_assets "
            "(work_id,revision,asset_id,role,position,alt,width,height) "
            "VALUES (?,?,?,'cover',0,?,?,?)",
            (work_id, revision, cover["asset_id"], cover["alt"], cover["width"], cover["height"]),
        )


def read_asset(store, work_id, item):
    try:
        if store is None:
            raise ValueError("Public asset store is not configured")
        return store.read(work_id, item)
    except (OSError, ValueError) as error:
        raise ApiError(
            503, "work_asset_unavailable", "这份公开文件暂时无法读取，请稍后重试。"
        ) from error


def validate_publication(connection, work_id, content, store):
    if not content["files"] and not content["links"]:
        raise ApiError(409, "work_no_outcome", "请先为作品选定实际访问链接或下载文件。")
    for item in selected_assets(connection, work_id, content):
        data = read_asset(store, work_id, item)
        cover = content["cover"]
        if cover and cover["asset_id"] == item["id"]:
            if png_dimensions(data) != (cover["width"], cover["height"]):
                raise ValueError("Cover dimensions differ from the PNG")


def public_cover(connection, work_id, revision):
    row = connection.execute(
        "SELECT asset_id,alt,width,height FROM public_work_revision_assets "
        "WHERE work_id=? AND revision=? AND role='cover'",
        (work_id, revision),
    ).fetchone()
    if row is None:
        return None
    return {
        **dict(row),
        "url": f"/api/works/{work_id}/assets/{row['asset_id']}/preview",
    }

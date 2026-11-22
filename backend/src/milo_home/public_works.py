"""Explicit local publication of whole work snapshots, anonymous current reads."""

import hashlib
import json
import time
from pathlib import Path
from uuid import UUID

from .errors import ApiError
from .public_asset_store import display_filename, source_snapshot
from .public_work_records import (
    SUMMARY_COLUMNS,
    append_revision,
    local_view,
    public_cover,
    require_version,
    saved_content,
    selections,
    validate_publication,
    work_entry,
)
from .public_works_models import PublicWorkInput


class PublicWorks:
    def __init__(self, database, *, store=None, clock=time.time):
        self.database, self.store, self.clock = database, store, clock

    def show(self, work_id):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            return local_view(connection, work_entry(connection, work_id))

    def create(self, work_id, content):
        work_id = str(UUID(work_id))
        content = PublicWorkInput.model_validate(content).model_dump(mode="json")
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            row = connection.execute("SELECT * FROM public_works WHERE id=?", (work_id,)).fetchone()
            if row is not None:
                if saved_content(connection, work_id, 1) != content:
                    raise ApiError(409, "work_exists", "这个作品身份已有不同内容。")
                return local_view(connection, row)
            # Deferred foreign keys let selections be complete before latest points here.
            append_revision(connection, work_id, 1, content, now)
            connection.execute(
                "INSERT INTO public_works (id,version,latest_revision,created_at,updated_at) "
                "VALUES (?,1,1,?,?)",
                (work_id, now, now),
            )
            return local_view(connection, work_entry(connection, work_id))

    def revise(self, work_id, content, expected_version):
        content = PublicWorkInput.model_validate(content).model_dump(mode="json")
        with self.database.connect(write=True) as connection:
            row = work_entry(connection, work_id)
            require_version(row, expected_version)
            if saved_content(connection, row["id"], row["latest_revision"]) == content:
                return local_view(connection, row)
            revision, now = row["latest_revision"] + 1, int(self.clock())
            append_revision(connection, row["id"], revision, content, now)
            connection.execute(
                "UPDATE public_works SET latest_revision=?,version=version+1,updated_at=? "
                "WHERE id=?",
                (revision, now, row["id"]),
            )
            return local_view(connection, work_entry(connection, row["id"]))

    def import_asset(self, work_id, asset_id, source, *, filename=None):
        work_id, asset_id = str(UUID(work_id)), str(UUID(asset_id))
        filename = display_filename(filename if filename is not None else source.name)
        with self.database.connect() as connection:
            work_entry(connection, work_id)
        data = source_snapshot(source)
        item = {
            "id": asset_id,
            "work_id": work_id,
            "filename": filename,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if self.store is None:
            raise ValueError("Configure the separate public asset store before importing")
        with self.database.connect(write=True) as connection:
            work_entry(connection, work_id)
            for workspace in connection.execute("SELECT root_path FROM workspaces"):
                self.store.check_workspace(Path(workspace["root_path"]))
            existing = connection.execute(
                "SELECT * FROM public_work_assets WHERE id=?", (asset_id,)
            ).fetchone()
            if existing is not None and any(existing[k] != v for k, v in item.items()):
                raise ApiError(
                    409, "work_asset_changed", "该文件身份已封存不同内容，请使用新身份。"
                )
            self.store.seal(work_id, item, data)
            if existing is None:
                connection.execute(
                    "INSERT INTO public_work_assets "
                    "(id,work_id,filename,size_bytes,sha256,created_at) VALUES (?,?,?,?,?,?)",
                    (*item.values(), int(self.clock())),
                )
            return dict(
                connection.execute(
                    "SELECT * FROM public_work_assets WHERE id=?", (asset_id,)
                ).fetchone()
            )

    def publish(self, work_id, expected_version):
        with self.database.connect(write=True) as connection:
            row = work_entry(connection, work_id)
            require_version(row, expected_version)
            content = saved_content(connection, row["id"], row["latest_revision"])
            validate_publication(connection, row["id"], content, self.store)
            if row["published_revision"] == row["latest_revision"]:
                return local_view(connection, row)
            sequence = (
                row["sequence"]
                or connection.execute(
                    "SELECT coalesce(max(sequence),0)+1 FROM public_works"
                ).fetchone()[0]
            )
            now = int(self.clock())
            connection.execute(
                "UPDATE public_works SET published_revision=latest_revision,sequence=?,"
                "published_at=coalesce(published_at,?),public_updated_at=?,"
                "updated_at=?,version=version+1 WHERE id=?",
                (sequence, now, now, now, row["id"]),
            )
            return local_view(connection, work_entry(connection, row["id"]))

    def withdraw(self, work_id, expected_version):
        with self.database.connect(write=True) as connection:
            row = work_entry(connection, work_id)
            require_version(row, expected_version)
            if row["published_revision"] is not None:
                connection.execute(
                    "UPDATE public_works SET published_revision=NULL,updated_at=?,"
                    "version=version+1 "
                    "WHERE id=?",
                    (int(self.clock()), row["id"]),
                )
            return local_view(connection, work_entry(connection, row["id"]))

    def list_works(self, *, limit=10, before=None):
        if not 1 <= limit <= 30 or (before is not None and not 1 <= before <= 9223372036854775807):
            raise ValueError("Invalid work page")
        clause, params = ("", []) if before is None else (" AND w.sequence<?", [before])
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                f"SELECT {SUMMARY_COLUMNS} FROM public_works w JOIN public_work_revisions r "
                "ON r.work_id=w.id AND r.revision=w.published_revision "
                f"WHERE w.published_revision IS NOT NULL{clause} "
                "ORDER BY w.sequence DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
            works = [
                {**dict(row), "cover": public_cover(connection, row["id"], row["revision"])}
                for row in rows[:limit]
            ]
        return {
            "works": works,
            "next_before": works[-1]["sequence"] if len(rows) > limit else None,
        }

    def get(self, work_id):
        work_id = str(UUID(work_id))
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                f"SELECT {SUMMARY_COLUMNS},r.body,r.credits,r.links_json FROM public_works w "
                "JOIN public_work_revisions r ON r.work_id=w.id "
                "AND r.revision=w.published_revision "
                "WHERE w.id=?",
                (work_id,),
            ).fetchone()
            if row is None:
                raise ApiError(404, "work_not_found", "这件作品当前不可读取。")
            result = dict(row)
            result["links"] = json.loads(result.pop("links_json"))
            result["cover"] = public_cover(connection, work_id, row["revision"])
            result["files"] = [
                {
                    "id": item["id"],
                    "filename": item["filename"],
                    "size_bytes": item["size_bytes"],
                    "sha256": item["sha256"],
                    "download_url": f"/api/works/{work_id}/assets/{item['id']}/download",
                }
                for item in selections(connection, work_id, row["revision"])
                if item["role"] == "download"
            ]
            return result

    def public_asset(self, work_id, asset_id, role):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT a.*,s.width,s.height FROM public_works w "
                "JOIN public_work_revision_assets s ON s.work_id=w.id "
                "AND s.revision=w.published_revision JOIN public_work_assets a "
                "ON a.work_id=s.work_id AND a.id=s.asset_id "
                "WHERE w.id=? AND a.id=? AND s.role=?",
                (work_id, asset_id, role),
            ).fetchone()
        if row is None:
            raise ApiError(404, "work_asset_not_found", "这份文件当前不在作品的公开版本中。")
        return dict(row)

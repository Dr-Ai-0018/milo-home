"""All registered public asset bytes, including unpublished and withdrawn revisions."""

import re
from pathlib import Path
from uuid import UUID

from .public_asset_store import MAX_FILE_BYTES, PublicAssetStore, display_filename
from .public_works_integrity import public_works_constraints_ok
from .workspace_archive_files import read_file, write_file

MAX_ENTRIES = 12000


def public_asset_index(database, budget):
    with database.connect() as connection:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA user_version").fetchone()[0] < 35:
            return []
        if not public_works_constraints_ok(connection):
            raise ValueError("Public work index constraints are incomplete")
        rows = connection.execute(
            "SELECT work_id,id,filename,size_bytes,sha256,created_at "
            "FROM public_work_assets ORDER BY work_id,id LIMIT ?",
            (MAX_ENTRIES + 1,),
        ).fetchall()
    result, used = [], 0
    for row in rows:
        item = dict(row)
        asset_name(item)
        display_filename(item["filename"])
        if (
            type(item["size_bytes"]) is not int
            or not 0 <= item["size_bytes"] <= MAX_FILE_BYTES
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        ):
            raise ValueError("Public asset index metadata is invalid")
        used += item["size_bytes"]
        if used > budget or len(result) >= MAX_ENTRIES:
            raise ValueError("Public asset index exceeds the archive budget")
        result.append(item)
    return result


def asset_name(item):
    work_id, asset_id = item["work_id"], item["id"]
    if str(UUID(work_id)) != work_id or str(UUID(asset_id)) != asset_id:
        raise ValueError("Public asset index contains a noncanonical identity")
    return f"public-assets/works/{work_id}/{asset_id}"


def _output_path(output, item):
    path = output
    parts = asset_name(item).split("/")
    for part in parts[:-1]:
        path = path / part
        path.mkdir(mode=0o700, exist_ok=True)
    return path / parts[-1]


def capture_public_assets(output, store, index):
    for item in index:
        write_file(_output_path(output, item), store.read(item["work_id"], item))


def verify_public_source(store, index):
    for item in index:
        store.read(item["work_id"], item)


def verify_public_archive(manifest, index):
    scope = manifest.get("public_assets")
    if manifest["version"] == 4:
        if (
            not isinstance(scope, dict)
            or set(scope) != {"source", "index"}
            or scope["index"] != index
        ):
            raise ValueError("Paired public asset scope differs from its database")
        source = scope["source"]
        if source is None:
            if index:
                raise ValueError("Paired public assets have no source")
        elif (
            not isinstance(source, str)
            or not Path(source).is_absolute()
            or ".." in Path(source).parts
        ):
            raise ValueError("Invalid public asset source")
    elif scope is not None or index:
        raise ValueError("Historical paired version cannot omit indexed public assets")
    expected = {
        asset_name(item): {
            "bytes": item["size_bytes"],
            "sha256": item["sha256"],
            "mode": 0o600,
        }
        for item in index
    }
    actual = {
        key: value
        for key, value in manifest["artifacts"].items()
        if key.startswith("public-assets/")
    }
    if actual != expected:
        raise ValueError("Public asset archive differs from the business index")


def restore_public_assets(archive, output, index, budget):
    if not index:
        return 0
    for item in index:
        data, _ = read_file(archive, asset_name(item), item["size_bytes"])
        path = _output_path(output, item)
        write_file(path, data)
        path.chmod(0o400)
    store = PublicAssetStore(output / "public-assets", max_bytes=budget)
    verify_public_source(store, index)
    return len(index)

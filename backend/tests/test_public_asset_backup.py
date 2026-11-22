"""Nonempty public assets survive complete generations without publishing drafts."""

import hashlib
import json
import sqlite3
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_paired_backup import capture
from test_paired_backup import paired_case as paired_case
from test_paired_native_replies import capture as capture_with_replies
from test_paired_native_replies import paired_reply_case as paired_reply_case
from test_public_assets import png
from test_public_works import content
from test_work_tasks import work_case as work_case

from milo_home.cli import main
from milo_home.database import Database
from milo_home.errors import ApiError
from milo_home.execution_lock import execution_lock
from milo_home.maintenance_backup import preflight_backup
from milo_home.maintenance_policy import BackupPolicy
from milo_home.maintenance_summary import directory_fact
from milo_home.paired_backup import rehearse_generation, verified_generation
from milo_home.public_asset_backup import public_asset_index
from milo_home.public_asset_store import PublicAssetStore
from milo_home.public_works import PublicWorks
from milo_home.work_attachment_batches import seal_attachments
from milo_home.work_attachment_store import AttachmentStore
from milo_home.work_tasks import WorkTasks
from milo_home.workspace_archive_files import RESERVE, json_bytes

BUDGET = 64 * 1024 * 1024


def seed(root, database):
    store_root = root / "public-store"
    store_root.mkdir(mode=0o700)
    store = PublicAssetStore(store_root, max_bytes=256 * 1024 * 1024)
    service = PublicWorks(database, store=store)
    source = root / "selected.bin"
    works = []
    for stage in ("pending", "draft", "withdrawn"):
        work = service.create(str(uuid4()), content(title="合成作品 " + stage))
        source.write_bytes(b"synthetic-outcome-" + stage.encode())
        asset = service.import_asset(work["id"], str(uuid4()), source, filename=stage + ".bin")
        cover = None
        if stage == "pending":
            source.write_bytes(png())
            image = service.import_asset(work["id"], str(uuid4()), source, filename="cover.png")
            cover = {"asset_id": image["id"], "alt": "合成封面", "width": 2, "height": 1}
        body = content(title="合成作品 " + stage, files=[asset["id"]], cover=cover)
        work = service.revise(work["id"], body, work["version"])
        if stage != "draft":
            work = service.publish(work["id"], work["version"])
        if stage == "withdrawn":
            work = service.withdraw(work["id"], work["version"])
        if stage == "pending":
            source.write_bytes(b"synthetic-pending-outcome")
            new = service.import_asset(work["id"], str(uuid4()), source, filename="pending-new.bin")
            work = service.revise(
                work["id"], content(files=[new["id"]], body="尚未公开的新稿"), work["version"]
            )
        works.append(work)
    source.unlink()
    return service, works


def round_trip(case, capture_, *, reply=False):
    root, database = case[:2]
    service, works = seed(root, database)
    expected = public_asset_index(database, BUDGET)
    assert len(expected) == 5
    original = {item["id"]: service.store.read(item["work_id"], item) for item in expected}
    captured = capture_(case, public_asset_store=service.store)
    archive = root / "archive"
    manifest = verified_generation(archive, captured["manifest_sha256"], BUDGET)
    assert manifest["version"] == 4
    assert ("replies" in manifest["components"]) is reply
    assert manifest["public_assets"]["index"] == expected
    (root / "public-store").rename(root / "original-store-away")
    output = root / "restored"
    result = rehearse_generation(archive, output, captured["manifest_sha256"], budget=BUDGET)
    assert result["public_assets_restored"] == 5 and not result["automatic_resume"]
    restored = PublicWorks(
        Database(output / "business.sqlite3"),
        store=PublicAssetStore(output / "public-assets", max_bytes=256 * 1024 * 1024),
    )
    assert public_asset_index(restored.database, BUDGET) == expected
    for item in expected:
        assert restored.store.read(item["work_id"], item) == original[item["id"]]
    for before in works:
        assert restored.show(before["id"]) == before
    assert restored.get(works[0]["id"])["revision"] == 2
    assert restored.show(works[0]["id"])["latest_revision"] == 3
    for before in works[1:]:
        with pytest.raises(ApiError) as error:
            restored.get(before["id"])
        assert error.value.status == 404
    assert result["file_restore_verified"]


def test_v4_nonempty_public_assets_restore_without_replies(paired_case):
    round_trip(paired_case, capture)


def test_v4_nonempty_public_assets_restore_with_native_reply_mapping(paired_reply_case):
    round_trip(paired_reply_case, capture_with_replies, reply=True)


def test_indexed_public_assets_require_store_before_any_output_is_created(paired_case):
    root, database, home, workspace = paired_case[:4]
    seed(root, database)
    with pytest.raises(ValueError, match="indexed public asset store"):
        capture(paired_case)
    assert not (root / "archive").exists()
    with pytest.raises(ValueError, match="indexed public asset store"):
        preflight_backup(
            database,
            home,
            workspace,
            root / "archive",
            BackupPolicy(BUDGET, include=("extra.txt",)),
        )


@pytest.mark.parametrize("damage", ["missing", "changed", "during-capture"])
def test_damaged_public_bytes_refuse_complete_generation(paired_case, monkeypatch, damage):
    import milo_home.paired_backup as module

    root, database = paired_case[:2]
    service, _ = seed(root, database)
    asset = public_asset_index(database, BUDGET)[0]
    path = service.store.root / "works" / asset["work_id"] / asset["id"]

    def damage_bytes():
        if damage == "missing":
            path.unlink()
        else:
            path.chmod(0o600)
            path.write_bytes(b"changed-byte-content")
            path.chmod(0o400)

    if damage == "during-capture":
        original = module.capture_components

        def change(*args):
            result = original(*args)
            damage_bytes()
            return result

        monkeypatch.setattr(module, "capture_components", change)
    else:
        damage_bytes()
    with pytest.raises((ValueError, OSError)):
        capture(paired_case, public_asset_store=service.store)
    assert (root / "archive/INCOMPLETE").exists()
    assert not (root / "archive/manifest.json").exists()


def test_v4_cannot_drop_public_scope_or_sneak_replies_into_component_version(paired_case):
    root, database = paired_case[:2]
    service, _ = seed(root, database)
    captured = capture(paired_case, public_asset_store=service.store)
    path = root / "archive/manifest.json"
    original = json.loads(path.read_text())
    for variation in ("missing-public", "old-version", "reply-source-only"):
        manifest = json.loads(json.dumps(original))
        if variation == "missing-public":
            manifest.pop("public_assets")
        elif variation == "old-version":
            manifest["version"] = 1
            manifest.pop("public_assets")
        else:
            manifest["reply_source"] = str(root / "not-included")
        data = json_bytes(manifest)
        path.write_bytes(data)
        with pytest.raises(ValueError):
            verified_generation(root / "archive", hashlib.sha256(data).hexdigest(), BUDGET)
    assert captured["captured"]


def test_public_capacity_counts_orphans_and_preflight_adds_four_copies(paired_case, monkeypatch):
    import milo_home.maintenance_backup as module

    root, database, home, workspace = paired_case[:4]
    service, _ = seed(root, database)
    item = public_asset_index(database, BUDGET)[0]
    pending = service.store.root / "works" / item["work_id"] / ".pending-interrupted"
    pending.write_bytes(b"unregistered-but-counted")
    fact = directory_fact("public_assets", service.store.root, limit=service.store.max_bytes)
    used = sum(asset["size_bytes"] for asset in public_asset_index(database, BUDGET))
    assert fact.complete and fact.used_bytes == used + pending.stat().st_size
    monkeypatch.setattr(module, "source_state", lambda *a, **kw: {"estimated_peak_bytes": 1000})
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * BUDGET))
    result = preflight_backup(
        database,
        home,
        workspace,
        root / "archive",
        BackupPolicy(BUDGET, include=("extra.txt",)),
        public_asset_store=service.store,
    )
    assert result["ready"] and result["estimated_peak_bytes"] == 1000 + 4 * used
    assert result["budget_bytes"] == BUDGET
    assert RESERVE <= BUDGET


@pytest.mark.parametrize("damage", ["missing-table", "missing-index"])
def test_new_schema_cannot_masquerade_as_empty_public_asset_index(paired_case, damage):
    database = paired_case[1]
    with database.connect() as connection:
        if damage == "missing-table":
            connection.execute("DROP TABLE public_work_assets")
        else:
            connection.execute("DROP INDEX public_work_asset_references")
    with pytest.raises((ValueError, sqlite3.Error)):
        public_asset_index(database, BUDGET)


def historical_roundtrip(case, captured, version):
    root = case[0]
    path = root / "archive/manifest.json"
    manifest = json.loads(path.read_text())
    manifest["version"] = version
    manifest.pop("public_assets")
    data = json_bytes(manifest)
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    result = rehearse_generation(root / "archive", root / "restored", digest, budget=BUDGET)
    assert result["generation"] == captured["generation"]
    assert result["file_restore_verified"] and not result["automatic_resume"]
    assert result["public_assets_restored"] == 0
    assert path.read_bytes() == data


def test_legacy_v1_empty_public_scope_restores_unchanged(paired_case):
    historical_roundtrip(paired_case, capture(paired_case), 1)


def test_legacy_v2_private_attachments_restore_unchanged(paired_case):
    root, database, _, workspace, _, intent = paired_case
    with database.connect() as connection:
        current = connection.execute(
            "SELECT owner_id,session_id FROM work_tasks WHERE id=?", (intent.task_id,)
        ).fetchone()
    task, _ = WorkTasks(database).create(
        current["owner_id"], current["session_id"], "合成附件恢复", str(uuid4())
    )
    with database.connect(write=True) as connection:
        connection.execute("UPDATE work_tasks SET status='completed' WHERE id=?", (task["id"],))
    handoff = workspace / "tasks" / task["id"] / "handoff"
    handoff.mkdir(parents=True)
    (handoff / "old.bin").write_bytes(b"legacy-private-attachment")
    store_root = root / "private-store"
    store_root.mkdir(mode=0o700)
    store = AttachmentStore(store_root, max_bytes=1024)
    with execution_lock(database) as lease:
        seal_attachments(database, current["owner_id"], task["id"], store=store, lease=lease)
    captured = capture(
        paired_case, attachment_store=store, exclude=[f"tasks/{task['id']}/handoff/old.bin"]
    )
    historical_roundtrip(paired_case, captured, 2)


def test_legacy_v3_native_reply_scope_restores_unchanged(paired_reply_case):
    historical_roundtrip(paired_reply_case, capture_with_replies(paired_reply_case), 3)


def test_public_asset_store_is_wired_through_real_backup_cli(paired_case, monkeypatch, capsys):
    root, database, home, workspace = paired_case[:4]
    service, _ = seed(root, database)
    monkeypatch.setenv("MILO_DATABASE", str(database.path))
    monkeypatch.setenv("MILO_PUBLIC_ASSET_ROOT", str(service.store.root))
    monkeypatch.setenv("MILO_PUBLIC_ASSET_MAX_BYTES", str(service.store.max_bytes))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "milo-home",
            "--run-directory",
            str(home),
            "backup-paired",
            "capture",
            "--workspace",
            str(workspace),
            "--writers-stopped",
            "--include",
            "extra.txt",
            "--out",
            str(root / "archive"),
            "--budget-bytes",
            str(BUDGET),
        ],
    )
    main()
    captured = json.loads(capsys.readouterr().out)
    manifest = verified_generation(root / "archive", captured["manifest_sha256"], BUDGET)
    assert len(manifest["public_assets"]["index"]) == 5
    assert manifest["public_assets"]["source"] == str(service.store.root)

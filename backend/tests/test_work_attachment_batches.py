import shutil

import pytest
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.database import Database
from milo_home.errors import ApiError
from milo_home.execution_lock import execution_lock
from milo_home.work_attachment_batches import (
    read_attachment,
    read_attachments,
    register_attachment_attempt,
    seal_attachments,
)
from milo_home.work_attachment_store import AttachmentStore


@pytest.fixture
def batch_case(work_case, tmp_path):
    database, owner, _, workspace_id, access, *_ = work_case
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    access.register_workspace(workspace_id, "合成工作区", workspace)
    task_id = create(work_case)["id"]
    with execution_lock(database) as lease:
        register_attachment_attempt(database, owner, task_id, lease=lease, clock=lambda: 102)
    with database.connect(write=True) as connection:
        connection.execute("UPDATE work_tasks SET status='completed' WHERE id=?", (task_id,))
    handoff = workspace / "tasks" / task_id / "handoff"
    handoff.mkdir(parents=True)
    root = tmp_path / "attachments"
    root.mkdir(mode=0o700)
    store = AttachmentStore(root, max_bytes=128)
    return database, owner, task_id, handoff, store


def _seal(batch_case):
    database, owner, task_id, _, store = batch_case
    with execution_lock(database) as lease:
        return seal_attachments(database, owner, task_id, store=store, lease=lease)


def test_sealed_batch_survives_source_changes_and_restores_same_bytes(batch_case, tmp_path):
    database, owner, task_id, handoff, store = batch_case
    first = handoff / "图像.bin"
    second = handoff / "LICENSE"
    first.write_bytes(b"\x00\xff")
    second.write_bytes(b"example")
    sealed = _seal(batch_case)
    assert sealed["state"] == "available"
    assert [item["filename"] for item in sealed["attachments"]] == ["LICENSE", "图像.bin"]

    first.write_bytes(b"edited")
    second.unlink()
    assert _seal(batch_case) == sealed

    backup = tmp_path / "restored.sqlite3"
    database.backup(backup)
    restored = Database(backup)
    restored.initialize()
    copies = tmp_path / "restored-attachments"
    shutil.copytree(store.root, copies)
    restored_store = AttachmentStore(copies, max_bytes=128)
    assert read_attachments(restored, owner, task_id) == sealed
    for item, data in zip(sealed["attachments"], (b"example", b"\x00\xff"), strict=True):
        assert read_attachment(database, owner, task_id, item["id"], store=store) == (item, data)
        assert read_attachment(restored, owner, task_id, item["id"], store=restored_store) == (
            item,
            data,
        )


def test_failed_batch_leaves_no_visible_index_and_can_retry(batch_case):
    database, owner, task_id, handoff, store = batch_case
    (handoff / "a").write_bytes(b"one")
    (handoff / "z").write_bytes(b"x" * 128)
    with pytest.raises(ValueError, match="capacity"):
        _seal(batch_case)
    assert read_attachments(database, owner, task_id) == {"state": "pending", "attachments": []}
    assert list((store.root / "tasks" / task_id).iterdir()) == []
    (handoff / "z").unlink()
    assert len(_seal(batch_case)["attachments"]) == 1


def test_read_rechecks_original_task_and_authorization(batch_case, work_case):
    database, owner, task_id, handoff, store = batch_case
    _, _, other, _, access, *_ = work_case
    (handoff / "file").write_bytes(b"example")
    attachment = _seal(batch_case)["attachments"][0]
    other_task = create(work_case)["id"]
    for user, task in [(other, task_id), (owner, other_task)]:
        with pytest.raises(ApiError, match="work_not_found"):
            read_attachment(database, user, task, attachment["id"], store=store)
    access.revoke(owner)
    access.grant(owner)
    with pytest.raises(ApiError, match="work_not_found"):
        read_attachment(database, owner, task_id, attachment["id"], store=store)


def test_unindexed_bytes_cannot_be_overwritten_on_retry(batch_case):
    database, owner, task_id, handoff, store = batch_case
    (handoff / "file").write_bytes(b"example")
    item = _seal(batch_case)["attachments"][0]
    with database.connect(write=True) as connection:
        connection.execute("DELETE FROM work_attachments WHERE task_id=?", (task_id,))
        connection.execute(
            "UPDATE work_attachment_batches SET state='pending' WHERE task_id=?", (task_id,)
        )
    saved = store.root / "tasks" / task_id / item["id"]
    saved.chmod(0o600)
    saved.write_bytes(b"changed")
    saved.chmod(0o400)
    with pytest.raises(ValueError, match="digest"):
        _seal(batch_case)
    assert saved.read_bytes() == b"changed"
    assert read_attachments(database, owner, task_id)["state"] == "pending"


def test_empty_handoff_is_not_an_attachment_failure(batch_case):
    assert _seal(batch_case) == {"state": "none", "attachments": []}

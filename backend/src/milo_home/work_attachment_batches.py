"""Seal one completed task as an indivisible, authorized attachment batch."""

import hashlib
import time
from uuid import UUID, uuid5

from .errors import ApiError
from .work_access import require_task, unavailable
from .work_artifacts import ready_workspace
from .work_attachment_source import handoff_files
from .work_decisions import latest


def _published(connection, task_id):
    batch = connection.execute(
        "SELECT state FROM work_attachment_batches WHERE task_id=?", (task_id,)
    ).fetchone()
    if batch is None:
        return None
    rows = connection.execute(
        "SELECT id, filename, size_bytes, sha256 FROM work_attachments "
        "WHERE task_id=? ORDER BY filename",
        (task_id,),
    )
    return {"state": batch["state"], "attachments": [dict(row) for row in rows]}


def enrolled(connection, task_id):
    return (
        connection.execute(
            "SELECT 1 FROM work_receipt_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        is not None
    )


def register_attachment_attempt(database, owner, task_id, *, lease, clock):
    lease.verify()
    with database.connect(write=True) as connection:
        require_task(connection, owner, task_id)
        if not enrolled(connection, task_id):
            return False
        connection.execute(
            "INSERT INTO work_attachment_batches (task_id,state,sealed_at) "
            "VALUES (?,'pending',?) ON CONFLICT(task_id) DO NOTHING",
            (task_id, int(clock())),
        )
        return True


def read_attachments(database, owner, task_id):
    with database.connect() as connection:
        connection.execute("BEGIN")
        task = require_task(connection, owner, task_id)
        batch = _published(connection, task_id)
        if batch is not None and batch["state"] != "pending":
            return batch
        decision = latest(connection, task_id)
        if (
            task["status"] == "completed"
            and decision is not None
            and decision["state"] != "accepted"
        ):
            return {"state": "none", "attachments": []}
        if task["status"] in {
            "failed",
            "cancelled",
            "timed_out",
        }:
            return {"state": "none", "attachments": []}
        if batch is not None or (task["status"] != "completed" and enrolled(connection, task_id)):
            return {"state": "pending", "attachments": []}
        return {"state": "none", "attachments": []}


def select_attachments(database, owner, task_id, attachment_ids):
    batch = read_attachments(database, owner, task_id)
    items = {row["id"]: row for row in batch["attachments"]}
    if attachment_ids is not None and any(key not in items for key in attachment_ids):
        raise unavailable()
    if not items:
        raise ApiError(409, "work_attachment_unavailable", "这张单暂时没有可下载的附件。")
    return (
        list(items.values()) if attachment_ids is None else [items[key] for key in attachment_ids]
    )


def read_sealed(store, task_id, item):
    try:
        return store.read(task_id, item)
    except (OSError, ValueError) as error:
        raise ApiError(409, "work_attachment_unavailable", "这份附件暂时取不回来。") from error


def recheck_attachment_access(database, owner, task_id):
    with database.connect() as connection:
        require_task(connection, owner, task_id)


def read_attachment(database, owner, task_id, attachment_id, *, store):
    (item,) = select_attachments(database, owner, task_id, [attachment_id])
    data = read_sealed(store, task_id, item)
    recheck_attachment_access(database, owner, task_id)
    return item, data


def _sealed_candidates(workspace, task_id, policy):
    for filename, data in handoff_files(workspace, task_id):
        if policy is not None and (
            not policy.permits(filename)
            or any(value.encode() in data for value in policy.sensitive)
        ):
            raise ValueError("Attachment contains a protected runtime value")
        digest = hashlib.sha256(data).hexdigest()
        yield (
            {
                "id": str(uuid5(UUID(task_id), f"{filename}:{digest}")),
                "filename": filename,
                "size_bytes": len(data),
                "sha256": digest,
            },
            data,
        )


def seal_attachments(database, owner, task_id, *, store, lease, clock=time.time, policy=None):
    """Only a local coordinator holding the shared execution lease may publish."""
    lease.verify()
    with database.connect() as connection:
        connection.execute("BEGIN")
        workspace = ready_workspace(connection, owner, task_id)
        store.check_workspace(workspace)
        current = _published(connection, task_id)
        if current is not None and current["state"] != "pending":
            return current
        if not enrolled(connection, task_id):
            raise ApiError(409, "work_attachment_legacy", "这张旧单仍使用原来的成果记录。")
    items = store.seal(task_id, _sealed_candidates(workspace, task_id, policy))
    lease.verify()
    with database.connect(write=True) as connection:
        if ready_workspace(connection, owner, task_id) != workspace:
            raise ValueError("Work directory changed while sealing")
        current = _published(connection, task_id)
        if current is not None and current["state"] != "pending":
            return current
        connection.execute(
            "INSERT INTO work_attachment_batches (task_id,state,sealed_at) VALUES (?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET state=excluded.state,sealed_at=excluded.sealed_at",
            (task_id, "available" if items else "none", int(clock())),
        )
        connection.executemany(
            "INSERT INTO work_attachments (task_id,id,filename,size_bytes,sha256) "
            "VALUES (:task_id,:id,:filename,:size_bytes,:sha256)",
            [dict(item, task_id=task_id) for item in items],
        )
        return _published(connection, task_id)


def record_attachment_failure(database, owner, task_id, *, lease, clock):
    lease.verify()
    with database.connect(write=True) as connection:
        ready_workspace(connection, owner, task_id)
        if enrolled(connection, task_id):
            connection.execute(
                "INSERT INTO work_attachment_batches (task_id,state,sealed_at) "
                "VALUES (?,'unavailable',?) ON CONFLICT(task_id) DO UPDATE SET "
                "state=excluded.state,sealed_at=excluded.sealed_at "
                "WHERE work_attachment_batches.state='pending'",
                (task_id, int(clock())),
            )

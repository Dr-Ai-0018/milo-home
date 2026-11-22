"""Keep exactly the indexed attachment bytes with their original database snapshot."""

from uuid import UUID

from .work_attachment_source import MAX_FILE_BYTES
from .work_attachment_store import AttachmentStore
from .workspace_archive_files import read_file, write_file

MAX_ENTRIES = 12000


def attachment_index(database, budget):
    with database.connect() as connection:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA user_version").fetchone()[0] < 24:
            return []
        if connection.execute(
            "SELECT 1 FROM work_attachment_batches b WHERE (state='available') != "
            "EXISTS (SELECT 1 FROM work_attachments a WHERE a.task_id=b.task_id)"
        ).fetchone():
            raise ValueError("Attachment index and batch state disagree")
        rows = connection.execute(
            "SELECT task_id,id,size_bytes,sha256 FROM work_attachments ORDER BY task_id,id LIMIT ?",
            (MAX_ENTRIES + 1,),
        ).fetchall()
    result = []
    used = 0
    for row in rows:
        item = dict(row)
        _name(item)
        if not 0 <= item["size_bytes"] <= MAX_FILE_BYTES:
            raise ValueError("Attachment index contains an invalid size")
        used += item["size_bytes"]
        if used > budget or len(result) == MAX_ENTRIES:
            raise ValueError("Indexed attachments exceed the archive budget")
        result.append(item)
    return result


def _name(item):
    task, identifier = item["task_id"], item["id"]
    if str(UUID(task)) != task or str(UUID(identifier)) != identifier:
        raise ValueError("Attachment index contains a noncanonical identity")
    return f"attachments/tasks/{task}/{identifier}"


def _archive_path(directory, item):
    path = directory
    parts = _name(item).split("/")
    for part in parts[:-1]:
        path = path / part
        path.mkdir(mode=0o700, exist_ok=True)
    return path / parts[-1]


def capture_attachments(output, store, index):
    for item in index:
        write_file(_archive_path(output, item), store.read(item["task_id"], item))


def verify_attachment_source(store, index):
    for item in index:
        store.read(item["task_id"], item)


def verify_attachment_archive(manifest, index):
    actual = {
        key: value for key, value in manifest["artifacts"].items() if key.startswith("attachments/")
    }
    expected = {
        _name(item): {"bytes": item["size_bytes"], "sha256": item["sha256"], "mode": 0o600}
        for item in index
    }
    if actual != expected:
        raise ValueError("Attachment archive differs from the business index")


def restore_attachments(archive, output, index, budget):
    if not index:
        return 0
    root = output / "attachments"
    root.mkdir(mode=0o700)
    restored = AttachmentStore(root, max_bytes=budget)
    for item in index:
        name = _name(item)
        path = _archive_path(output, item)
        data, _ = read_file(archive, name, item["size_bytes"])
        write_file(path, data)
        path.chmod(0o400)
    verify_attachment_source(restored, index)
    return len(index)

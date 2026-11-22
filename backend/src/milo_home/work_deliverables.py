"""A task can hand back one small text file in its own workspace directory."""

from uuid import UUID

from .work_access import require_task
from .work_artifacts import read_snapshot, ready_workspace, save_snapshot


def validate_filename(value: str) -> str:
    if (
        not 5 <= len(value.encode("utf-8")) <= 100
        or not value.endswith(".txt")
        or not value[0].isalnum()
        or not all(char.isalnum() or char in "._-" for char in value)
    ):
        raise ValueError("请填写一个 .txt 文件名，只用文字、数字、点、下划线或短横线。")
    return value


def deliverable_path(task_id: str, filename: str) -> str:
    return f"deliverables/{UUID(task_id)}/{validate_filename(filename)}"


def expected_deliverable(connection, task_id):
    return connection.execute(
        "SELECT filename,observation FROM work_deliverables WHERE task_id=?", (task_id,)
    ).fetchone()


def inspect_deliverable(root, path, policy):
    try:
        text, digest = read_snapshot(root, path)
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError):
        return "withheld", None
    if not policy.permits(text.replace("\r\n", "\n")):
        return "withheld", None
    return "saved", (text, digest)


def collect_deliverable(database, owner, task_id, *, lease, clock, policy):
    lease.verify()
    with database.connect() as connection:
        connection.execute("BEGIN")
        require_task(connection, owner, task_id)
        expected = expected_deliverable(connection, task_id)
        if expected is None or expected["observation"] is not None:
            return
        root = ready_workspace(connection, owner, task_id)
    path = deliverable_path(task_id, expected["filename"])
    observation, snapshot = inspect_deliverable(root, path, policy)
    lease.verify()
    with database.connect(write=True) as connection:
        if ready_workspace(connection, owner, task_id) != root:
            raise ValueError("Workspace changed during deliverable collection")
        current = expected_deliverable(connection, task_id)
        if current != expected:
            return
        if snapshot is not None:
            text, digest = snapshot
            save_snapshot(connection, task_id, path, text, digest, int(clock()))
        connection.execute(
            "UPDATE work_deliverables SET observation=? WHERE task_id=?", (observation, task_id)
        )


def deliverable_view(connection, task_id):
    expected = expected_deliverable(connection, task_id)
    if expected is None:
        return None
    path = deliverable_path(task_id, expected["filename"])
    saved = connection.execute(
        "SELECT 1 FROM work_artifacts WHERE task_id=? AND path=?", (task_id, path)
    ).fetchone()
    return {
        "filename": expected["filename"],
        "path": path,
        "state": "saved" if saved else (expected["observation"] or "pending"),
    }

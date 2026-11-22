from io import BytesIO
from zipfile import ZipFile

from test_work_api import create, initialize
from test_work_api import http_case as http_case

from milo_home.execution_lock import execution_lock
from milo_home.work_attachment_batches import seal_attachments
from milo_home.work_attachment_store import AttachmentStore


def test_http_serves_original_bytes_and_never_returns_a_partial_archive(http_case, tmp_path):
    client, owner, _, workspace_id, tokens = http_case
    session = initialize(http_case)
    state = client.app.state
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state.work_access.register_workspace(workspace_id, "合成工作区", workspace)
    task, _ = create(client, session)
    other, _ = create(client, session)
    task_id = task["id"]
    path = f"/api/work-tasks/{task_id}/attachments"
    assert client.get(path).json() == {"state": "none", "attachments": []}
    root = tmp_path / "files"
    root.mkdir(mode=0o700)
    store = state.work_attachment_store = AttachmentStore(root, max_bytes=1024)
    assert client.get(path).json()["state"] == "pending"
    handoff = workspace / "tasks" / task_id / "handoff"
    handoff.mkdir(parents=True)
    (handoff / "图.html").write_bytes(b"<html>")
    (handoff / "notes").write_bytes(b"notes")
    with state.database.connect(write=True) as connection:
        connection.execute("UPDATE work_tasks SET status='completed' WHERE id=?", (task_id,))
    # A completed task from before attachment enablement cannot wait for a batch forever.
    assert client.get(path).json() == {"state": "none", "attachments": []}
    with execution_lock(state.database) as lease:
        seal_attachments(state.database, owner, task_id, store=store, lease=lease)
    listing = client.get(path).json()
    assert listing["state"] == "available"
    notes, html = listing["attachments"]
    download = f"{path}/{html['id']}/download"
    archive = f"{path}/archive"
    (handoff / "图.html").write_bytes(b"changed")
    response = client.get(download)
    assert response.status_code == 200
    assert response.content == b"<html>"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    for selection, expected in [(None, ["notes", "图.html"]), ([html["id"]], ["图.html"])]:
        response = client.post(archive, json={"attachment_ids": selection})
        assert response.status_code == 200
        with ZipFile(BytesIO(response.content)) as package:
            assert package.namelist() == expected
            assert package.read("图.html") == b"<html>"
    for invalid in ([], [html["id"], html["id"]]):
        assert client.post(archive, json={"attachment_ids": invalid}).status_code == 422
    wrong_task = f"/api/work-tasks/{other['id']}/attachments/archive"
    assert client.post(wrong_task, json={"attachment_ids": [html["id"]]}).status_code == 404
    client.cookies.set("milo_session", tokens[1])
    assert client.get(download).status_code == 404
    assert client.post(archive, json={"attachment_ids": None}).status_code == 404
    client.cookies.set("milo_session", tokens[0])
    (root / "tasks" / task_id / notes["id"]).unlink()
    assert client.post(archive, json={"attachment_ids": None}).status_code == 409
    assert client.get(download).status_code == 200
    state.work_access.revoke(owner)
    assert client.get(download).status_code == 404

import hashlib
import os

import pytest
from test_work_api import create, initialize
from test_work_api import http_case as http_case
from test_work_migration import snapshot

from milo_home import database as database_module
from milo_home.errors import ApiError
from milo_home.work_artifacts import publish_artifact, read_snapshot


def test_saved_bytes_are_private_and_survive_file_changes(http_case, tmp_path):
    client, owner, _, _, tokens = http_case
    session = initialize(http_case)
    task, _ = create(client, session)
    unrelated, _ = create(client, session)
    task_id = task["id"]
    base = f"/api/work-tasks/{task_id}/artifacts"
    database = client.app.state.database
    text = "第一行\r\n<script>plain text</script>\n"
    path = tmp_path / "deliverable.txt"
    path.write_bytes(text.encode())
    sha = hashlib.sha256(text.encode()).hexdigest()
    with pytest.raises(ApiError, match="work_artifact_not_ready"):
        publish_artifact(database, owner, task_id, path.name, sha)
    assert client.get(base).json() == {"artifacts": []}
    assert client.post(base, json={"path": path.name}).status_code == 405
    with database.connect(write=True) as connection:
        connection.execute("UPDATE work_tasks SET status='completed' WHERE id=?", (task_id,))
    with pytest.raises(ApiError, match="work_artifact_changed"):
        publish_artifact(database, owner, task_id, path.name, "0" * 64)
    receipt = publish_artifact(database, owner, task_id, path.name, sha)
    assert receipt["created"]
    assert publish_artifact(database, owner, task_id, path.name, sha) == {
        **receipt,
        "created": False,
    }
    path.write_text("later", encoding="utf-8")
    with pytest.raises(ApiError, match="work_artifact_exists"):
        publish_artifact(database, owner, task_id, path.name, hashlib.sha256(b"later").hexdigest())
    path.unlink()
    saved = client.get(base)
    assert saved.headers["cache-control"] == "no-store"
    assert saved.json() == {
        "artifacts": [
            {"path": path.name, "text": text, "sha256": sha, "checked_at": receipt["checked_at"]}
        ]
    }
    assert client.get(f"/api/work-tasks/{unrelated['id']}/artifacts").json() == {"artifacts": []}
    client.cookies.set("milo_session", tokens[1])
    assert client.get(base).status_code == 404
    client.cookies.set("milo_session", tokens[0])
    client.app.state.work_access.revoke(owner)
    client.app.state.work_access.grant(owner)
    assert client.get(base).status_code == 404
    client.post("/api/auth/logout")
    assert client.get(base).status_code == 401


def test_file_boundary_excludes_unsafe_and_incomplete_reads(tmp_path):
    root = tmp_path / "desk"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    (root / "linked").symlink_to(tmp_path, target_is_directory=True)
    os.link(outside, root / "hard.txt")
    os.mkfifo(root / "pipe.txt")
    (root / "huge.txt").write_bytes(b"a" * 16_385)
    (root / "binary.txt").write_bytes(b"a\x00")
    (root / "utf8.txt").write_bytes(b"\xff")
    (root / ".hidden").mkdir()
    (root / ".hidden/note.txt").write_text("private", encoding="utf-8")
    for path in (
        str(outside),
        "../outside.txt",
        "link.txt",
        "linked/outside.txt",
        "hard.txt",
        "pipe.txt",
        "huge.txt",
        "binary.txt",
        "utf8.txt",
        ".hidden/note.txt",
    ):
        with pytest.raises((OSError, ValueError)):
            read_snapshot(root, path)
    (root / "note.txt").write_bytes(b"\n  indented\n")
    assert read_snapshot(root, "note.txt")[0] == "\n  indented\n"


def test_v19_upgrade_adds_no_evidence_to_existing_database(tmp_path, monkeypatch):
    database = database_module.Database(tmp_path / "v19.sqlite3")
    with monkeypatch.context() as old:
        old.setattr(database_module, "MIGRATIONS", database_module.MIGRATIONS[:19])
        database.initialize()
    before = snapshot(database)
    database.initialize()
    after = snapshot(database)
    assert after.pop("work_artifacts") == []
    assert after.pop("work_deliverables") == []
    assert after == before
    with database.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

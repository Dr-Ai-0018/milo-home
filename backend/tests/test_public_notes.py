import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from milo_home.database import Database
from milo_home.errors import ApiError
from milo_home.public_notes import PublicNotes
from milo_home.public_notes_models import PublicNoteDetail, PublicNotePage


def content(**changes):
    return {
        "title": "一条合成公开近况",
        "summary": "明确写好的合成摘要。",
        "body": "合成正文第一段。\n\n第二段 <script>只是文字</script>。",
        "category": None,
        "content_date": "2026-11-21",
        **changes,
    }


def setup(client):
    now = [1795329000]
    service = client.app.state.public_notes
    service.clock = lambda: now[0]
    return service, now


def publish(service, **changes):
    note_id = str(uuid4())
    draft = service.create(note_id, content(**changes))
    return service.publish(note_id, draft["version"])


def test_complete_lifecycle_preserves_public_version_until_explicit_publish(client):
    service, now = setup(client)
    note_id = str(uuid4())
    draft = service.create(note_id, content(), source_key="synthetic-private-origin")
    missing = client.get(f"/api/notes/{uuid4()}")
    assert client.get(f"/api/notes/{note_id}").json() == missing.json()
    assert client.get("/api/notes").json() == {"notes": [], "next_before": None}
    first = service.publish(note_id, draft["version"])
    public = client.get(f"/api/notes/{note_id}").json()
    assert public["body"] == content()["body"]
    assert public["revision"] == 1
    assert not {"source_key", "version", "latest", "published"} & public.keys()
    assert client.get("/api/notes").headers["cache-control"] == "no-store"
    now[0] += 60
    pending = service.revise(note_id, content(body="新版尚未公开。"), first["version"])
    assert pending["latest_revision"] == 2 and pending["published_revision"] == 1
    assert client.get(f"/api/notes/{note_id}").json() == public
    changed = service.publish(note_id, pending["version"])
    current = client.get(f"/api/notes/{note_id}").json()
    assert current["body"] == "新版尚未公开。" and current["revision"] == 2
    assert current["sequence"] == public["sequence"]
    assert current["published_at"] == public["published_at"]
    assert current["updated_at"] == now[0]
    withdrawn = service.withdraw(note_id, changed["version"])
    assert client.get(f"/api/notes/{note_id}").status_code == 404
    assert client.get("/api/notes").json()["notes"] == []
    with pytest.raises(ApiError) as stale:
        service.publish(note_id, pending["version"])
    assert stale.value.code == "note_changed"
    retried_create = service.create(note_id, content(), source_key="synthetic-private-origin")
    assert retried_create["version"] == withdrawn["version"]
    assert retried_create["published_revision"] is None
    now[0] += 60
    reopened = service.publish(note_id, withdrawn["version"])
    assert reopened["sequence"] == first["sequence"]
    assert reopened["published_at"] == first["published_at"]


def test_withdraw_keeps_pending_draft_and_old_commands_cannot_republish(client):
    service, _ = setup(client)
    first = publish(service)
    draft = service.revise(first["id"], content(title="待发标题"), first["version"])
    withdrawn = service.withdraw(first["id"], draft["version"])
    assert withdrawn["latest"]["title"] == "待发标题"
    with pytest.raises(ApiError):
        service.publish(first["id"], draft["version"])
    reopened = service.publish(first["id"], withdrawn["version"])
    assert reopened["published_revision"] == 2
    assert service.get(first["id"])["title"] == "待发标题"


def test_pagination_filters_before_limit_and_is_stable_across_insert_withdraw(client):
    service, _ = setup(client)
    notes = [publish(service) for _ in range(5)]
    service.withdraw(notes[3]["id"], notes[3]["version"])
    first = client.get("/api/notes?limit=2").json()
    assert [n["sequence"] for n in first["notes"]] == [5, 3]
    assert first["next_before"] == 3
    publish(service)
    service.withdraw(notes[1]["id"], notes[1]["version"])
    second = client.get("/api/notes?limit=2&before=3").json()
    assert [n["sequence"] for n in second["notes"]] == [1]
    assert second["next_before"] is None
    assert client.get("/api/notes?before=1").json() == {"notes": [], "next_before": None}
    assert "body" not in first["notes"][0]


def test_concurrent_first_publications_have_distinct_persistent_sequences(client):
    service, _ = setup(client)
    drafts = [service.create(str(uuid4()), content()) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda row: service.publish(row["id"], 1), drafts))
    assert {row["sequence"] for row in results} == {1, 2, 3, 4}
    for row in results:
        service.withdraw(row["id"], row["version"])
    assert publish(service)["sequence"] == 5


def test_read_api_requires_no_login_and_has_no_write_or_revision_preview(client):
    service, _ = setup(client)
    row = publish(service)
    assert client.get("/api/auth/me").status_code == 401
    assert client.get(f"/api/notes/{row['id']}").status_code == 200
    assert client.post("/api/notes", json=content()).status_code == 405
    for query in ["limit=0", "limit=31", "before=0", "before=wrong"]:
        assert client.get("/api/notes?" + query).status_code == 422
    assert client.get("/api/notes/not-a-uuid").status_code == 422
    with pytest.raises(ApiError):
        service.revise(row["id"], content(title="冲突"), row["version"] - 1)
    with pytest.raises(ApiError):
        service.create(row["id"], content(title="冲突"))


def test_backup_reopen_retains_draft_public_pointer_and_withdrawn_history(client, tmp_path):
    service, _ = setup(client)
    visible = publish(service)
    service.revise(visible["id"], content(body="待发布草稿。"), visible["version"])
    gone = publish(service)
    service.withdraw(gone["id"], gone["version"])
    target = tmp_path / "copy.sqlite3"
    service.database.backup(target)
    restored = PublicNotes(Database(target))
    assert restored.get(visible["id"])["body"] == content()["body"]
    assert restored.show(visible["id"])["latest"]["body"] == "待发布草稿。"
    with pytest.raises(ApiError):
        restored.get(gone["id"])
    with restored.database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE public_note_revisions SET body='改写历史'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE public_notes SET sequence=999 WHERE sequence=1")


def test_all_shared_success_samples_match_real_response_models():
    path = Path(__file__).resolve().parents[2] / "examples/public-notes.json"
    data = json.loads(path.read_text())
    assert data["synthetic"] is True
    assert len(data["scenarios"]) == 12
    for scenario in data["scenarios"]:
        if scenario["status"] == 200:
            payload = scenario["response"]
            model = PublicNotePage if "notes" in payload else PublicNoteDetail
            assert model.model_validate(payload).model_dump(mode="json") == payload

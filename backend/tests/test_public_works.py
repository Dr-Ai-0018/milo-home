"""Whole-snapshot publication, stable identity and anonymous data boundaries."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import ValidationError

from milo_home.database import Database
from milo_home.errors import ApiError
from milo_home.public_asset_store import PublicAssetStore
from milo_home.public_works import PublicWorks
from milo_home.public_works_integrity import public_works_constraints_ok
from milo_home.public_works_models import PublicWorkDetail, PublicWorkInput, PublicWorkPage


def content(**changes):
    return {
        "title": "合成作品",
        "summary": "供隔离核对的作品。",
        "body": "介绍第一段。\n\n<script>只是纯文本</script>",
        "content_date": "2026-11-22",
        "links": [{"label": "访问示例", "url": "https://example.org/"}],
        **changes,
    }


@pytest.fixture
def service(tmp_path):
    tmp_path.chmod(0o700)
    database = Database(tmp_path / "home.sqlite3")
    database.initialize()
    root = tmp_path / "assets"
    root.mkdir(mode=0o700)
    return PublicWorks(database, store=PublicAssetStore(root, max_bytes=256 * 1024 * 1024))


def published(service, **changes):
    draft = service.create(str(uuid4()), content(**changes))
    return service.publish(draft["id"], draft["version"])


def test_lifecycle_preserves_whole_public_snapshot_and_stale_commands_cannot_revive(service):
    draft = service.create(str(uuid4()), content())
    assert draft["published"] is None
    assert service.list_works() == {"works": [], "next_before": None}
    with pytest.raises(ApiError) as missing:
        service.get(draft["id"])
    assert missing.value.status == 404
    first = service.publish(draft["id"], 1)
    public = service.get(first["id"])
    PublicWorkDetail.model_validate(public)
    assert not {"version", "latest_revision", "published_revision", "assets"} & public.keys()
    revised = service.revise(first["id"], content(body="新草稿", version_label="冬日版"), 2)
    assert revised["latest_revision"] == 2 and revised["published_revision"] == 1
    assert service.get(first["id"]) == public
    current = service.publish(first["id"], revised["version"])
    assert service.get(first["id"])["version_label"] == "冬日版"
    assert current["sequence"] == first["sequence"]
    withdrawn = service.withdraw(first["id"], current["version"])
    with pytest.raises(ApiError) as stale:
        service.publish(first["id"], revised["version"])
    assert stale.value.status == 409
    assert service.create(first["id"], content())["published"] is None
    restored = service.publish(first["id"], withdrawn["version"])
    assert restored["published_at"] == first["published_at"]
    assert restored["sequence"] == first["sequence"]
    with service.database.connect() as connection:
        assert public_works_constraints_ok(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM public_works WHERE id=?", (first["id"],))


def test_selection_is_explicit_atomic_and_cross_work_ids_are_rejected(service, tmp_path):
    first = published(service)
    source = tmp_path / "说明.pdf"
    source.write_bytes(b"synthetic-file-not-a-real-pdf")
    asset = service.import_asset(first["id"], str(uuid4()), source)
    assert service.show(first["id"])["version"] == first["version"]
    assert service.get(first["id"])["files"] == []
    second = service.create(str(uuid4()), content())
    with pytest.raises(ApiError):
        service.revise(second["id"], content(files=[asset["id"]]), 1)
    revision = service.revise(first["id"], content(files=[asset["id"]]), first["version"])
    assert service.get(first["id"])["files"] == []
    source.unlink()
    public = service.publish(first["id"], revision["version"])
    assert service.get(first["id"])["files"][0]["sha256"] == asset["sha256"]
    assert service.store.read(first["id"], asset) == b"synthetic-file-not-a-real-pdf"
    with service.database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE public_work_assets SET filename='other' WHERE id=?", (asset["id"],)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO public_work_revision_assets "
                "(work_id,revision,asset_id,role,position) VALUES (?,1,?,'download',0)",
                (first["id"], asset["id"]),
            )
    assert public["published_revision"] == 2


def test_failed_publish_retains_old_snapshot_and_empty_outcome_is_not_public(service, tmp_path):
    empty = service.create(str(uuid4()), content(links=[]))
    with pytest.raises(ApiError) as error:
        service.publish(empty["id"], 1)
    assert error.value.code == "work_no_outcome"
    first = published(service)
    source = tmp_path / "file.bin"
    source.write_bytes(b"original")
    asset = service.import_asset(first["id"], str(uuid4()), source)
    pending = service.revise(first["id"], content(files=[asset["id"]]), first["version"])
    (service.store.root / "works" / first["id"] / asset["id"]).unlink()
    with pytest.raises(ApiError) as error:
        service.publish(first["id"], pending["version"])
    assert error.value.status == 503
    assert service.get(first["id"])["revision"] == 1
    assert service.show(first["id"])["version"] == pending["version"]


def test_pagination_filters_before_limit_and_first_sequences_never_repeat(service):
    works = [published(service) for _ in range(5)]
    service.withdraw(works[3]["id"], works[3]["version"])
    first = service.list_works(limit=2)
    PublicWorkPage.model_validate(first)
    assert [w["sequence"] for w in first["works"]] == [5, 3]
    assert first["next_before"] == 3
    assert published(service)["sequence"] == 6
    service.withdraw(works[1]["id"], works[1]["version"])
    last = service.list_works(limit=2, before=3)
    assert [w["sequence"] for w in last["works"]] == [1]
    assert last["next_before"] is None


def test_concurrent_first_publications_serialize_sequences(service):
    drafts = [service.create(str(uuid4()), content()) for _ in range(3)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda d: service.publish(d["id"], 1), drafts))
    assert sorted(row["sequence"] for row in results) == [1, 2, 3]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org",
        "//example.org",
        "https://u:p@example.org",
        "https://example.org/\nsecret",
        "https://example.org\\@other/",
        "https://",
        "https://example.org:99999/",
    ],
)
def test_invalid_link_structures_are_rejected_without_fetch(url):
    with pytest.raises(ValidationError):
        PublicWorkInput.model_validate(content(links=[{"label": "访问", "url": url}]))

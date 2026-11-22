"""Real anonymous route boundary, byte responses and publication races."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_public_assets import png
from test_public_works import content

from milo_home.app import create_app
from milo_home.config import PublicAssetSettings, Settings
from milo_home.public_works_routes import DOWNLOAD_SLOTS


@pytest.fixture
def public_client(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    assets = tmp_path / "assets"
    assets.mkdir(mode=0o700)
    settings = Settings(
        database=data / "home.sqlite3",
        origins=("https://home.test",),
        public_assets=PublicAssetSettings(assets, 256 * 1024 * 1024),
    )
    with TestClient(create_app(settings), base_url="https://home.test") as client:
        yield client


def prepare(client, tmp_path):
    service = client.app.state.public_works
    work = service.create(str(uuid4()), content())
    source = tmp_path / "合成说明.html"
    source.write_bytes(b"<script>synthetic-download-only</script>")
    file = service.import_asset(work["id"], str(uuid4()), source)
    source.write_bytes(png())
    image = service.import_asset(work["id"], str(uuid4()), source, filename="cover.png")
    revised = service.revise(
        work["id"],
        content(
            files=[file["id"]],
            cover={
                "asset_id": image["id"],
                "alt": "合成封面",
                "width": 2,
                "height": 1,
            },
        ),
        1,
    )
    public = service.publish(work["id"], revised["version"])
    return service, public, file, image


def test_anonymous_current_reads_serve_fixed_bytes_without_starting_execution(
    public_client, tmp_path
):
    client = public_client
    service, work, file, image = prepare(client, tmp_path)
    detail = client.get(f"/api/works/{work['id']}")
    assert detail.status_code == 200
    value = detail.json()
    assert "latest_revision" not in value and "created_at" not in value
    assert value["body"] == content()["body"]
    assert client.get("/api/works?limit=2").json()["works"][0]["id"] == work["id"]
    response = client.get(value["files"][0]["download_url"])
    assert response.content == b"<script>synthetic-download-only</script>"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment; filename*=UTF-8''")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    preview = client.get(value["cover"]["url"])
    assert preview.content == png() and preview.headers["content-type"] == "image/png"
    assert client.get(f"/api/works/{work['id']}/assets/{file['id']}/preview").status_code == 404
    assert client.get(f"/api/works/{work['id']}/assets/{image['id']}/download").status_code == 404
    other = service.create(str(uuid4()), content())
    service.publish(other["id"], 1)
    assert client.get(f"/api/works/{other['id']}/assets/{file['id']}/download").status_code == 404
    with service.database.connect() as connection:
        for table in ("runs", "work_tasks", "sessions", "work_sessions"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_current_asset_reuse_then_old_url_withdrawal_and_missing_bytes_are_distinct(
    public_client, tmp_path
):
    client = public_client
    service, work, file, _ = prepare(client, tmp_path)
    value = client.get(f"/api/works/{work['id']}").json()
    url = value["files"][0]["download_url"]
    pending = service.revise(
        work["id"], content(files=[file["id"]], body="新介绍"), work["version"]
    )
    assert client.get(url).status_code == 200
    work = service.publish(work["id"], pending["version"])
    assert client.get(url).status_code == 200
    path = service.store.root / "works" / work["id"] / file["id"]
    path.chmod(0o600)
    path.write_bytes(b"corrupted-but-kept")
    path.chmod(0o400)
    failure = client.get(url)
    assert failure.status_code == 503
    assert str(path) not in failure.text
    assert client.get(f"/api/works/{work['id']}").json()["body"] == "新介绍"
    pending = service.revise(work["id"], content(), work["version"])
    work = service.publish(work["id"], pending["version"])
    assert client.get(url).status_code == 404
    service.withdraw(work["id"], work["version"])
    assert client.get(f"/api/works/{work['id']}").status_code == 404
    assert client.get(url).status_code == 404


def test_withdraw_between_read_and_send_blocks_bytes_and_releases_slot(
    public_client, tmp_path, monkeypatch
):
    client = public_client
    service, work, _, _ = prepare(client, tmp_path)
    url = service.get(work["id"])["files"][0]["download_url"]
    original = service.store.read

    def withdraw(*args):
        data = original(*args)
        service.withdraw(work["id"], work["version"])
        return data

    monkeypatch.setattr(service.store, "read", withdraw)
    response = client.get(url)
    assert response.status_code == 404
    assert b"synthetic-download-only" not in response.content
    assert DOWNLOAD_SLOTS.acquire(blocking=False)
    assert DOWNLOAD_SLOTS.acquire(blocking=False)
    DOWNLOAD_SLOTS.release()
    DOWNLOAD_SLOTS.release()


def test_independent_download_slots_return_503_and_json_reads_remain_available(
    public_client, tmp_path
):
    service, work, _, _ = prepare(public_client, tmp_path)
    url = service.get(work["id"])["files"][0]["download_url"]
    assert DOWNLOAD_SLOTS.acquire(blocking=False)
    assert DOWNLOAD_SLOTS.acquire(blocking=False)
    try:
        assert public_client.get(url).status_code == 503
        assert public_client.get(f"/api/works/{work['id']}").status_code == 200
    finally:
        DOWNLOAD_SLOTS.release()
        DOWNLOAD_SLOTS.release()


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=31", "before=0", "before=no", "before=9223372036854775808"]
)
def test_invalid_public_page_parameters_are_422(public_client, query):
    assert public_client.get("/api/works?" + query).status_code == 422


def test_anonymous_has_no_write_route_and_no_paths_or_draft_preview(public_client):
    response = public_client.post("/api/works", json={}, headers={"Origin": "https://home.test"})
    assert response.status_code == 405
    assert public_client.get("/api/works/not-a-uuid").status_code == 422
    assert public_client.get("/api/works").json() == {"works": [], "next_before": None}

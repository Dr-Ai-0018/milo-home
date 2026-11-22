"""Self-contained synthetic assets exercise the real public-content CLI."""

import base64
import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from milo_home.cli import main
from milo_home.database import Database
from milo_home.public_asset_store import PublicAssetStore, png_dimensions
from milo_home.public_works import PublicWorks
from milo_home.public_works_models import PublicWorkInput


@pytest.fixture
def materials(tmp_path):
    examples = json.loads((Path(__file__).parents[2] / "examples/public-works.json").read_text())
    assert examples["synthetic"] is True
    files = []
    for item in examples["binary_fixtures"][:2]:
        data = base64.b64decode(item["body_base64"], validate=True)
        assert len(data) == item["size_bytes"]
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        name = (
            "synthetic-cover.png" if item["content_type"] == "image/png" else "synthetic-file.dat"
        )
        path = tmp_path / name
        path.write_bytes(data)
        files.append((str(uuid4()), path, item))
    draft = PublicWorkInput(
        title="合成作品：公开内容联验",
        summary="合成文件，仅供隔离验证。",
        body="这是一份合成正文，不是已发布的真实作品。",
        content_date="2026-11-22",
        credits="合成测试",
        links=[{"label": "示例", "url": "https://example.org/"}],
    ).model_dump(mode="json")
    work = draft | {
        "files": [files[1][0]],
        "cover": {"asset_id": files[0][0], "alt": "合成双格封面", "width": 2, "height": 1},
    }
    for name, content in (("draft", draft), ("work", work)):
        PublicWorkInput.model_validate(content)
        (tmp_path / (name + ".json")).write_text(json.dumps(content, ensure_ascii=False))
    return tmp_path, files, work


def test_synthetic_public_materials_are_rebuildable_and_match_declared_bytes(materials):
    _, files, _ = materials
    assert png_dimensions(files[0][1].read_bytes()) == (2, 1)
    assert files[1][1].read_bytes() == b"synthetic-download\n"


def test_synthetic_work_can_be_explicitly_imported_published_and_read_in_isolation(
    materials, tmp_path, monkeypatch, capsys
):
    material, files, content = materials
    data, assets = tmp_path / "data", tmp_path / "assets"
    data.mkdir(mode=0o700)
    assets.mkdir(mode=0o700)
    database = Database(data / "home.sqlite3")
    database.initialize()
    monkeypatch.setenv("MILO_DATABASE", str(database.path))
    monkeypatch.setenv("MILO_PUBLIC_ASSET_ROOT", str(assets))
    monkeypatch.setenv("MILO_PUBLIC_ASSET_MAX_BYTES", "268435456")
    identity = str(uuid4())

    def run(*arguments):
        monkeypatch.setattr(sys, "argv", ["milo-home", "content", "work", *map(str, arguments)])
        main()
        return json.loads(capsys.readouterr().out)

    draft = run("create", "--id", identity, "--file", material / "draft.json")
    assert draft["published"] is None
    for asset, path, item in files:
        saved = run("asset", "import", "--id", identity, "--asset-id", asset, "--file", path)
        assert saved["sha256"] == item["sha256"]
    revised = run(
        "revise",
        "--id",
        identity,
        "--file",
        material / "work.json",
        "--expected-version",
        draft["version"],
    )
    public = run(
        "publish", "--id", identity, "--expected-version", revised["version"], "--confirm-public"
    )
    assert public["published"]["title"] == content["title"]
    service = PublicWorks(database, store=PublicAssetStore(assets, max_bytes=256 * 1024**2))
    detail = service.get(identity)
    assert detail["credits"] == content["credits"] and detail["links"] == content["links"]
    assert detail["cover"]["width"] == 2
    assert service.store.read(identity, detail["files"][0]) == files[1][1].read_bytes()

"""Shared page fixtures, actual OpenAPI and independent capacity classification."""

import base64
import hashlib
import json
from pathlib import Path

import pytest
from test_public_works import content
from test_public_works import service as service

from milo_home.app import create_app
from milo_home.config import PublicAssetSettings, Settings, WorkAttachmentSettings
from milo_home.maintenance_models import MaintenanceView
from milo_home.public_asset_store import png_dimensions
from milo_home.public_works_models import PublicWorkDetail, PublicWorkInput, PublicWorkPage
from milo_home.schemas import ErrorEnvelope

ROOT = Path(__file__).parents[2]


def test_all_shared_fixtures_match_real_response_models_and_binary_metadata():
    fixture = json.loads((ROOT / "examples/public-works.json").read_text())
    assert fixture["synthetic"]
    assert len(fixture["scenarios"]) == 18
    for case in fixture["scenarios"]:
        body = case["response"]
        model = (
            ErrorEnvelope
            if case["status"] != 200
            else (PublicWorkPage if "works" in body else PublicWorkDetail)
        )
        assert model.model_validate(body).model_dump(mode="json") == body
    for asset in fixture["binary_fixtures"]:
        data = base64.b64decode(asset["body_base64"], validate=True)
        assert len(data) == asset["size_bytes"]
        assert hashlib.sha256(data).hexdigest() == asset["sha256"]
        if asset["content_type"] == "image/png":
            assert png_dimensions(data) == (2, 1)
    input_ = json.loads((ROOT / "examples/public-work-input.json").read_text())
    assert PublicWorkInput.model_validate(input_).model_dump(mode="json") == input_


def test_committed_openapi_is_from_actual_app_without_creating_database(tmp_path):
    path = tmp_path / "absent.sqlite3"
    actual = create_app(Settings(database=path, origins=("https://home.test",))).openapi()
    assert actual == json.loads((ROOT / "contracts/openapi.json").read_text())
    assert not path.exists()
    assert {p for p in actual["paths"] if p == "/api/works" or p.startswith("/api/works/")} == {
        "/api/works",
        "/api/works/{work_id}",
        "/api/works/{work_id}/assets/{asset_id}/download",
        "/api/works/{work_id}/assets/{asset_id}/preview",
    }


def test_capacity_type_can_represent_tenth_category_without_claiming_measurement():
    kinds = [
        "business",
        "attachments",
        "public_assets",
        "workspace",
        "journal_hot",
        "journal_cold",
        "native_mapping",
        "native_history",
        "backups",
        "development",
    ]
    summary = MaintenanceView(
        observed_at=1, expires_at=2, capacity=[{"kind": kind} for kind in kinds]
    )
    public = next(f for f in summary.capacity if f.kind == "public_assets")
    assert public.state == "unknown" and public.used_bytes is None


def test_asset_configuration_is_explicit_and_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("MILO_PUBLIC_ASSET_ROOT", str(tmp_path / "assets"))
    monkeypatch.delenv("MILO_PUBLIC_ASSET_MAX_BYTES", raising=False)
    with pytest.raises(ValueError):
        PublicAssetSettings.from_env()
    for budget in (0, -1):
        with pytest.raises(ValueError):
            PublicAssetSettings(tmp_path / "assets", budget)
    with pytest.raises(ValueError, match="separate"):
        Settings(
            database=tmp_path / "data" / "home.sqlite3",
            origins=("https://home.test",),
            public_assets=PublicAssetSettings(tmp_path / "data", 1024),
        )
    # Existing deployment keeps DB and both independent asset roots under one parent.
    settings = Settings(
        database=tmp_path / "data" / "home.sqlite3",
        origins=("https://home.test",),
        public_assets=PublicAssetSettings(tmp_path / "data" / "public-assets", 1024),
        work_attachments=WorkAttachmentSettings(tmp_path / "data" / "private-assets", 2048),
    )
    assert settings.public_assets.max_bytes == 1024
    assert settings.work_attachments.max_bytes == 2048
    with pytest.raises(ValueError, match="separate"):
        Settings(
            database=tmp_path / "data" / "home.sqlite3",
            origins=("https://home.test",),
            public_assets=PublicAssetSettings(tmp_path / "assets", 1024),
            work_attachments=WorkAttachmentSettings(tmp_path / "assets" / "private", 1024),
        )


def test_registered_workspace_cannot_double_as_public_asset_store(service, tmp_path):
    from uuid import uuid4

    work = service.create(str(uuid4()), content())
    source = tmp_path / "explicit.bin"
    source.write_bytes(b"synthetic")
    with service.database.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO workspaces VALUES (?,1,?,?,1,1,1)",
            (str(uuid4()), "synthetic workspace", str(tmp_path)),
        )
    with pytest.raises(ValueError, match="overlap"):
        service.import_asset(work["id"], str(uuid4()), source)
    assert service.show(work["id"])["assets"] == []

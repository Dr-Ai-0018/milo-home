"""HTTP reads cached owner-bound facts without reaching runtime or executing work."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from conftest import activate

from milo_home.config import MaintenanceSettings
from milo_home.maintenance_cache import publish_summary, read_summary
from milo_home.maintenance_models import (
    BackupFact,
    CapacityFact,
    MaintenanceDocument,
    MaintenanceView,
    age_summary,
)

NOW = 1795233600
SCENES = json.loads((Path(__file__).parents[2] / "examples/maintenance-status.json").read_text())


def summary():
    return MaintenanceView(
        observed_at=NOW,
        expires_at=NOW + 3600,
        capacity=[CapacityFact(kind="journal_hot", state="normal", used_bytes=100, complete=True)],
        backups=[
            BackupFact(
                kind="daily",
                state="normal",
                last_attempt_at=NOW - 10,
                last_success_at=NOW - 10,
                last_outcome="succeeded",
                fresh_until=NOW + 86400,
            )
        ],
    )


@pytest.fixture
def selected(client, tmp_path):
    user = activate(client)[0].json()
    root = tmp_path / "published"
    root.mkdir(mode=0o750)
    chosen = MaintenanceSettings(root / "summary.json", UUID(user["id"]))
    client.app.state.settings = replace(client.app.state.settings, maintenance=chosen)
    identity = client.app.state.settings.database.stat()
    document = MaintenanceDocument(
        owner_id=user["id"],
        database_device=identity.st_dev,
        database_inode=identity.st_ino,
        summary=summary(),
    )
    publish_summary(chosen.file, document)
    return chosen, document


def test_api_requires_login_selected_owner_and_current_grant(
    client, selected, tmp_path, monkeypatch
):
    monkeypatch.setattr("milo_home.maintenance_routes.time.time", lambda: NOW)
    chosen, _ = selected
    assert client.get("/api/maintenance").status_code == 403
    access = client.app.state.work_access
    access.register_workspace(str(uuid4()), "合成工作桌", tmp_path)
    access.grant(str(chosen.owner))
    response = client.get("/api/maintenance")
    assert response.status_code == 200
    assert response.json()["capacity"][0]["used_bytes"] == 100
    assert "database_inode" not in response.text and str(tmp_path) not in response.text
    access.revoke(str(chosen.owner))
    assert client.get("/api/maintenance").status_code == 403
    activate(client, "another")
    assert client.get("/api/maintenance").status_code == 403
    client.post("/api/auth/logout")
    assert client.get("/api/maintenance").status_code == 401


def test_unconfigured_endpoint_and_missing_cache_are_honest(client, selected):
    chosen, _ = selected
    chosen.file.unlink()
    value = read_summary(chosen, client.app.state.settings.database, now=NOW)
    assert value.state == "unknown" and value.expires_at == NOW
    client.app.state.settings = replace(client.app.state.settings, maintenance=None)
    assert client.get("/api/maintenance").status_code == 404


def test_expired_or_future_observation_never_renews_validity():
    for now in (NOW - 1, NOW + 3600):
        value = age_summary(summary(), now)
        assert value.state == "unknown" and value.capacity[0].state == "unknown"
        assert value.backups[0].last_success_at == NOW - 10
        assert value.expires_at == NOW + 3600
    value = summary()
    value.backups[0].fresh_until = NOW
    assert age_summary(value, NOW).backups[0].state == "attention"


def test_failed_attempt_keeps_previous_success():
    value = summary()
    fact = value.backups[0]
    fact.state = "attention"
    fact.last_outcome = "failed"
    fact.last_attempt_at = NOW
    fact.fresh_until = NOW - 1
    assert age_summary(value, NOW).state == "attention"
    assert age_summary(value, NOW).backups[0].reason != "expired"
    assert fact.last_success_at == NOW - 10


def test_document_from_another_database_or_owner_becomes_unknown(client, selected):
    chosen, document = selected
    document.database_inode += 1
    publish_summary(chosen.file, document)
    assert read_summary(chosen, client.app.state.settings.database, now=NOW).state == "unknown"
    document.owner_id = uuid4()
    publish_summary(chosen.file, document)
    assert read_summary(chosen, client.app.state.settings.database, now=NOW).state == "unknown"


def test_untrusted_files_and_raw_fields_are_rejected(client, selected):
    chosen, document = selected
    chosen.file.chmod(0o666)
    with pytest.raises(ValueError):
        read_summary(chosen, client.app.state.settings.database, now=NOW)
    publish_summary(chosen.file, document)
    chosen.file.write_text('{"private_path":"must never be echoed"}')
    with pytest.raises(ValueError):
        read_summary(chosen, client.app.state.settings.database, now=NOW)
    chosen.file.unlink()
    chosen.file.symlink_to(client.app.state.settings.database)
    with pytest.raises((ValueError, OSError)):
        read_summary(chosen, client.app.state.settings.database, now=NOW)


def test_policy_environment_requires_explicit_pair(monkeypatch, tmp_path):
    monkeypatch.delenv("MILO_MAINTENANCE_OWNER", raising=False)
    monkeypatch.setenv("MILO_MAINTENANCE_FILE", str(tmp_path / "summary.json"))
    with pytest.raises(ValueError):
        MaintenanceSettings.from_env()


@pytest.mark.parametrize("case", SCENES["cases"], ids=lambda case: case["name"])
def test_shared_scenes_use_actual_http_contract(case):
    assert SCENES["synthetic"] is True
    MaintenanceView.model_validate(case["payload"])

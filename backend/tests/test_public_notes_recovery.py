"""Recover real content states through existing business and paired backup entry points."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_paired_backup import capture
from test_paired_backup import paired_case as paired_case
from test_public_notes import content
from test_work_tasks import work_case as work_case

import milo_home.database_inspection as inspection
import milo_home.paired_backup as paired
import milo_home.paired_backup_state as state
from milo_home.backup_rehearsal import rehearse_backup
from milo_home.database import CURRENT_SCHEMA_VERSION, Database
from milo_home.database_inspection import inspect_database
from milo_home.errors import ApiError
from milo_home.paired_backup import rehearse_generation, verified_generation
from milo_home.paired_backup_state import business_state
from milo_home.public_notes import PublicNotes
from milo_home.public_notes_legacy import LEGACY_NOTES, import_legacy, legacy_id
from milo_home.public_notes_models import PublicNoteInput

TABLES = ("public_notes", "public_note_revisions")
POST_V33_TABLES = TABLES + (
    "public_work_revision_assets",
    "public_work_assets",
    "public_work_revisions",
    "public_works",
)
BUDGET = 64 * 1024 * 1024


def seed_notes(database):
    service = PublicNotes(database)
    imported = import_legacy(service)
    assert len(imported["created"]) == 3
    first, second = [legacy_id(note["key"]) for note in LEGACY_NOTES[:2]]
    service.revise(
        first, content(body="合成待发草稿，旧公开版本继续可读。"), service.show(first)["version"]
    )
    service.withdraw(second, service.show(second)["version"])
    draft = str(uuid4())
    service.create(draft, content(body="合成未公开记录，不得混入公开读取。"))
    return first, second, draft


def contents(database):
    with database.connect() as connection:
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
            for table in TABLES
        }


def assert_restored(database, expected, identities):
    first, withdrawn, draft = identities
    assert contents(database) == expected
    report = inspect_database(database.path)
    assert report["database_ok"] and report["public_notes_constraints_ok"]
    service = PublicNotes(database)
    assert service.show(first)["latest"]["body"] != service.get(first)["body"]
    for note_id in [withdrawn, draft]:
        with pytest.raises(ApiError) as error:
            service.get(note_id)
        assert error.value.status == 404
    before = contents(database)
    repeated = import_legacy(service)
    assert not repeated["created"] and len(repeated["skipped"]) == 3
    assert contents(database) == before


def test_online_backup_rehearsal_retains_every_content_state_and_original_source(client, tmp_path):
    database = client.app.state.database
    identities = seed_notes(database)
    expected = contents(database)
    original = business_state(database, BUDGET)
    output = tmp_path / "restored.sqlite3"
    report = rehearse_backup(database.path, output)
    assert report["record_counts"]["public_notes"] == 4
    assert report["record_counts"]["public_note_revisions"] == 5
    assert not report["source_migrated"] and not report["automatic_wakeup"]
    assert_restored(Database(output), expected, identities)
    assert business_state(database, BUDGET) == original


def test_paired_generation_retains_public_draft_withdrawn_order_and_full_digest(paired_case):
    root, database, _, _, _, _ = paired_case
    identities = seed_notes(database)
    expected = contents(database)
    captured = capture(paired_case)
    manifest = verified_generation(root / "archive", captured["manifest_sha256"], BUDGET)
    assert manifest["baseline"]["business"]["record_counts"]["public_notes"] == 4
    report = rehearse_generation(
        root / "archive", root / "restored", captured["manifest_sha256"], budget=BUDGET
    )
    assert report["file_restore_verified"]
    assert not report["automatic_resume"] and not report["remote_verified"]
    restored = Database(root / "restored/business.sqlite3")
    assert_restored(restored, expected, identities)
    assert contents(database) == expected
    # Same row counts do not hide body changes in the whole-database fingerprint.
    first = identities[0]
    with database.connect(write=True) as connection:
        connection.execute("DROP TRIGGER public_note_revision_immutable")
    original_state = business_state(database, BUDGET)
    with database.connect(write=True) as connection:
        connection.execute(
            "UPDATE public_note_revisions SET body='different synthetic content' "
            "WHERE note_id=? AND revision=2",
            (first,),
        )
    changed = business_state(database, BUDGET)
    assert changed["record_counts"] == original_state["record_counts"]
    assert changed["sha256"] != original_state["sha256"]


def test_v33_paired_archive_stays_historical_while_separate_copy_can_upgrade(
    paired_case, monkeypatch
):
    root, database, _, _, _, _ = paired_case
    # Remove every later content table rather than relabeling the current schema.
    with database.connect() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        for table in POST_V33_TABLES:
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=33")
        connection.commit()
    with monkeypatch.context() as old:
        for module in [inspection, paired, state]:
            old.setattr(module, "CURRENT_SCHEMA_VERSION", 33)
        old.setattr(
            inspection,
            "RECORD_TABLES",
            tuple(n for n in inspection.RECORD_TABLES if n not in POST_V33_TABLES),
        )
        captured = capture(paired_case)
    archive = root / "archive"
    original = business_state(Database(archive / "business.sqlite3"), BUDGET, archive_schema=33)
    report = rehearse_generation(
        archive, root / "restored", captured["manifest_sha256"], budget=BUDGET
    )
    assert report["business"]["schema_version"] == 33
    assert not (set(TABLES) & report["business"]["record_counts"].keys())
    upgraded = rehearse_backup(archive / "business.sqlite3", root / "upgraded.sqlite3")
    assert upgraded["copied_schema_version"] == 33
    assert upgraded["schema_version"] == CURRENT_SCHEMA_VERSION
    assert all(upgraded["record_counts"][table] == 0 for table in TABLES)
    assert (
        business_state(Database(archive / "business.sqlite3"), BUDGET, archive_schema=33)
        == original
    )


@pytest.mark.parametrize("damage", ["missing_trigger", "head_behind"])
def test_doctor_and_rehearsal_reject_lost_publication_constraints(client, tmp_path, damage):
    database = client.app.state.database
    first, _, _ = seed_notes(database)
    with database.connect(write=True) as connection:
        if damage == "missing_trigger":
            connection.execute("DROP TRIGGER public_note_revision_immutable")
        else:
            connection.execute("UPDATE public_notes SET latest_revision=1 WHERE id=?", (first,))
    before = contents(database)
    report = inspect_database(database.path)
    assert not report["database_ok"] and not report["public_notes_constraints_ok"]
    assert report["attention"] == ["public_notes_constraints_failed"]
    assert "合成待发草稿" not in json.dumps(report, ensure_ascii=False)
    with pytest.raises(ValueError, match="failed inspection"):
        rehearse_backup(database.path, tmp_path / "rejected.sqlite3")
    assert contents(database) == before


def test_legacy_import_exactly_matches_shipped_public_text():
    path = Path(__file__).resolve().parents[2] / "frontend/src/data/homeNotes.ts"
    source = path.read_text()
    for note in LEGACY_NOTES:
        for field in ("title", "category"):
            assert f"{field}: '{note[field]}'" in source
        assert f"text: '{note['text']}'" in source
    sample = Path(__file__).resolve().parents[2] / "examples/public-note-input.json"
    PublicNoteInput.model_validate_json(sample.read_text())

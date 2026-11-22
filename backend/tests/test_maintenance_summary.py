"""Actual directory observations and isolated failures in a mixed report."""

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from milo_home import maintenance_summary as summary
from milo_home.config import MaintenanceSettings
from milo_home.maintenance_evidence import load_facts, record_fact


def test_directory_bounds_links_and_distinct_business_bytes(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "real.bin").write_bytes(b"real")
    (workspace / "outside").symlink_to(tmp_path)
    used, _, complete = summary.bounded_size(workspace)
    assert used == 4 and complete is False
    assert summary.bounded_size(workspace, max_entries=1)[2] is False
    database = tmp_path / "home.sqlite3"
    database.write_bytes(b"database")
    (tmp_path / "attachments.bin").write_bytes(b"not part of sqlite")
    fact = summary.business_fact(SimpleNamespace(path=database))
    assert fact.used_bytes == 8 and fact.complete


def test_native_history_excludes_runtime_and_auth_but_includes_archive_and_state(tmp_path):
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "one.jsonl").write_bytes(b"one")
    (tmp_path / "archived_sessions").mkdir()
    (tmp_path / "archived_sessions" / "two.jsonl").write_bytes(b"two")
    (tmp_path / "state_5.sqlite").write_bytes(b"state")
    (tmp_path / "auth.json").write_bytes(b"private secret")
    (tmp_path / "session-journal.sqlite3").write_bytes(b"separate runtime")
    fact = summary.native_history_fact(tmp_path)
    assert fact.used_bytes == 11 and fact.complete


def test_sqlite_runtime_failure_does_not_hide_other_facts(monkeypatch, tmp_path):
    @contextmanager
    def lease(_):
        yield SimpleNamespace(verify=lambda: None)

    def broken():
        raise sqlite3.DatabaseError("private source error")

    monkeypatch.setattr(summary, "execution_lock", lease)
    monkeypatch.setattr(summary, "verify_identity", lambda *_: None)
    runtime = SimpleNamespace(database=None, journal=SimpleNamespace(capacity=broken))
    facts = summary.runtime_facts(runtime)
    assert len(facts) == 3
    assert all(fact.state == "unknown" and fact.reason == "evidence_invalid" for fact in facts)
    assert "private source" not in str(facts)


def test_backup_facts_keep_success_without_inventing_remote(private_runtime_directory):
    directory = private_runtime_directory("milo-maintenance-facts-")
    root = directory / "published"
    root.mkdir(mode=0o750)
    settings = MaintenanceSettings(root / "summary.json", uuid4())
    path = directory / "home.sqlite3"
    path.write_bytes(b"metadata only")
    database = SimpleNamespace(path=path)
    record_fact(
        settings,
        database,
        "paired",
        {
            "last_attempt_at": 100,
            "last_success_at": 110,
            "last_outcome": "succeeded",
            "generation_id": "a" * 32,
        },
        now=110,
    )
    fact = record_fact(
        settings,
        database,
        "paired",
        {
            "last_attempt_at": 200,
            "last_outcome": "failed",
        },
        now=200,
    )
    assert fact.last_success_at == 110 and fact.last_outcome == "failed"
    assert fact.state == "attention" and fact.generation_id == "a" * 32
    loaded = load_facts(settings, database)
    assert len(loaded) == 1 and loaded[0].kind == "paired"
    assert loaded[0].snapshot_id is None


def test_new_generation_does_not_inherit_old_remote_snapshot(private_runtime_directory):
    directory = private_runtime_directory("milo-maintenance-generation-")
    settings = MaintenanceSettings(directory / "summary.json", uuid4())
    path = directory / "home.sqlite3"
    path.write_bytes(b"metadata only")
    database = SimpleNamespace(path=path)
    first = {
        "last_attempt_at": 100,
        "last_success_at": 110,
        "last_outcome": "succeeded",
        "generation_id": "a" * 32,
        "snapshot_id": "b" * 64,
    }
    record_fact(settings, database, "remote", first, now=110)
    second = {**first, "last_attempt_at": 200, "last_success_at": 210}
    second.pop("snapshot_id")
    same = record_fact(settings, database, "remote", second, now=210)
    assert same.snapshot_id == "b" * 64
    second.update(last_attempt_at=300, last_success_at=310, generation_id="c" * 32)
    changed = record_fact(settings, database, "remote", second, now=310)
    assert changed.generation_id == "c" * 32 and changed.snapshot_id is None

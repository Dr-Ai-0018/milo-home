"""Local retention never starts work or weakens the existing archival gates."""

import os
from types import SimpleNamespace

import pytest
from test_execution_control import execution_case as execution_case
from test_journal_retention_scope import successful_work
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case

from milo_home import maintenance_retention as maintenance
from milo_home.execution_lock import execution_lock
from milo_home.journal_capacity import STREAM_LIMIT, TOTAL_LIMIT
from milo_home.native_messages import NativeMessageStore

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Host lease requires root")


@pytest.fixture
def settled(launch_case, fake_units, monkeypatch):
    successful_work(launch_case)
    monkeypatch.setattr(maintenance, "TRIGGER_BYTES", 1)
    return launch_case


def version(journal):
    with journal._connect() as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


def test_preview_is_read_only_and_real_archive_preserves_original_bytes_and_sequences(settled):
    _, runtime, journal, _, requests, _ = settled
    stream = journal.list_streams()[0]["stream"]["id"]
    before = journal.stream_snapshot(stream)
    traffic = requests.read_bytes()
    report = maintenance.preview(runtime)
    candidate = report["candidates"][0]
    assert report["status"] == "preview" and candidate["eligible"]
    assert report["state"]["segmentation_required"]
    assert version(journal) == 2 and journal.stream_snapshot(stream) == before
    result = maintenance.run(runtime)
    assert result["status"] == "maintained" and len(result["archived"]) == 1
    assert result["state"]["capacity"]["used_bytes"] == 0
    assert not result["state"]["segmentation_required"] and version(journal) == 3
    assert (journal.path.parent / "journal-segments" / (stream + ".json")).stat().st_size == (
        candidate["segment_bytes"]
    )
    assert journal.stream_snapshot(stream) == before
    assert requests.read_bytes() == traffic
    assert maintenance.run(runtime)["status"] == "below_threshold"
    assert maintenance.preview(runtime)["candidates"] == []


def test_low_water_updates_state_without_scanning_or_migrating(settled, monkeypatch):
    _, runtime, journal, *_ = settled
    monkeypatch.setattr(maintenance, "TRIGGER_BYTES", TOTAL_LIMIT - 2 * STREAM_LIMIT)
    monkeypatch.setattr(
        maintenance, "_candidate", lambda *_: pytest.fail("Low water must not scan candidates")
    )
    result = maintenance.run(runtime)
    assert result["status"] == "below_threshold"
    assert result["trigger_bytes"] == 16 * 1024 * 1024
    assert result["state"]["capacity"]["can_open_stream"]
    assert not result["archived"] and version(journal) == 2


def test_two_stream_bound_and_pagination_exclude_existing_archives(settled, monkeypatch):
    _, runtime, journal, *_ = settled
    successful_work(settled)
    successful_work(settled)
    streams = {row["stream"]["id"] for row in journal.list_streams()}
    snapshots = {stream: journal.stream_snapshot(stream) for stream in streams}
    first = maintenance.preview(runtime, limit=1)
    second = maintenance.preview(runtime, limit=1, after=first["next_after"])
    assert not first["scan_complete"] and not second["scan_complete"]
    assert first["candidates"][0]["stream_id"] != second["candidates"][0]["stream_id"]
    inspected = []
    candidate = maintenance._candidate

    def inspect(*args):
        inspected.append(args[1]["id"])
        return candidate(*args)

    monkeypatch.setattr(maintenance, "_candidate", inspect)
    result = maintenance.run(runtime)
    assert len(result["archived"]) == 2
    assert len(inspected) == 4  # Two candidates and their fresh preflights, never a third.
    assert not result["scan_complete"] and result["next_after"]
    assert result["state"]["capacity"]["used_bytes"] > 0
    assert len(maintenance.preview(runtime)["candidates"]) == 1
    assert len(maintenance.run(runtime)["archived"]) == 1
    assert maintenance.preview(runtime)["scan_complete"]
    assert maintenance.preview(runtime)["candidates"] == []
    assert {stream: journal.stream_snapshot(stream) for stream in streams} == snapshots


def test_busy_host_immediately_defers_without_claim_or_migration(launch_case):
    _, runtime, journal, *_ = launch_case
    with execution_lock(runtime.database):
        for operation in (maintenance.preview, maintenance.run):
            result = operation(runtime)
            assert result["status"] == "deferred"
            assert result["blocked_by"] == ["worker_busy"]
            assert not result["archived"]
    assert version(journal) == 2 and not journal.list_streams()


def test_unknown_delivery_remains_protected_and_is_not_migrated(settled):
    _, runtime, journal, *_ = settled
    with journal._connect() as connection:
        connection.execute("UPDATE requests SET state='unknown' WHERE method='turn/start'")
    report = maintenance.run(runtime)
    assert report["status"] == "no_eligible_candidates"
    assert report["candidates"][0]["blocked_by"] == ["protocol_requests_incomplete"]
    assert not report["archived"] and version(journal) == 2
    with journal._connect() as connection:
        assert connection.execute("SELECT count(*) FROM requests WHERE state='unknown'").fetchone()[
            0
        ]


def test_mapping_cold_space_and_orphans_block_before_archival(settled, monkeypatch):
    _, runtime, journal, *_ = settled
    with monkeypatch.context() as patch:
        patch.setattr(NativeMessageStore, "capacity", lambda _: {"has_capacity": False})
        result = maintenance.run(runtime)
        assert result["blocked_by"] == ["native_mapping_capacity"]
    with monkeypatch.context() as patch:
        patch.setattr(maintenance, "STORAGE_LIMIT", 0)
        result = maintenance.run(runtime)
        assert result["blocked_by"] == ["cold_storage_capacity"]
    with monkeypatch.context() as patch:
        patch.setattr(maintenance.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
        result = maintenance.run(runtime)
        assert result["blocked_by"] == ["maintenance_disk_space"]
    root = journal.path.parent / "journal-segments"
    root.mkdir(mode=0o700)
    orphan = root / "synthetic-unregistered.json"
    orphan.write_bytes(b"Keep this evidence")
    result = maintenance.run(runtime)
    assert result["blocked_by"] == ["unregistered_segment_evidence"]
    assert orphan.read_bytes() == b"Keep this evidence"
    assert result["state"]["orphan_count"] == 1
    assert version(journal) == 2 and not result["archived"]


def test_original_archive_rechecks_qualification_after_preflight(settled, monkeypatch):
    _, runtime, journal, *_ = settled
    archive = maintenance.journal_retention.archive

    def changed(runtime, stream_id, **options):
        with journal._connect() as connection:
            connection.execute("UPDATE requests SET state='unknown' WHERE method='turn/start'")
        return archive(runtime, stream_id, **options)

    monkeypatch.setattr(maintenance.journal_retention, "archive", changed)
    result = maintenance.run(runtime)
    assert result["status"] == "blocked"
    assert result["blocked_by"] == ["protocol_requests_incomplete"]
    assert not result["archived"] and version(journal) == 2


def test_deadline_prevents_starting_an_archive(settled, monkeypatch):
    _, runtime, journal, *_ = settled
    clock = iter((0, 0, 21))
    monkeypatch.setattr(maintenance.time, "monotonic", lambda: next(clock))
    result = maintenance.run(runtime)
    assert result["status"] == "deferred"
    assert result["blocked_by"] == ["maintenance_deadline"]
    assert not result["archived"] and version(journal) == 2


@pytest.mark.parametrize("options", [{"limit": 65}, {"after": "../private"}, {"max_streams": 3}])
def test_unbounded_or_invalid_invocations_are_rejected(options):
    with pytest.raises(ValueError):
        maintenance.run(None, **options)

"""Exhausted protected cold storage blocks fresh rounds while preserving cleanup."""

import os
from uuid import uuid4

import pytest
from test_journal_retention_scope import successful_reply
from test_native_reply_execution import native_reply_case as native_reply_case
from test_native_reply_execution import tick
from test_reply_execution import ready, send
from test_reply_readiness import preparation as preparation
from test_work_process import fake_units as fake_units

from milo_home import journal_segments
from milo_home.codex_rpc import ProtocolError
from milo_home.journal_capacity import can_admit
from milo_home.journal_retention import archive
from milo_home.reply_readiness import check_reply_service
from milo_home.work_runtime import WorkRuntime

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Native admission requires host lease")


@pytest.mark.parametrize("boundary", ["bytes", "count"])
def test_real_cold_limit_blocks_readiness_worker_and_shared_stream_admission(
    native_reply_case, client, monkeypatch, boundary
):
    successful_reply(native_reply_case, client)
    case, journal, requests, *_ = native_reply_case
    database, owner, _, _, directory, profile, key = case[0]
    runtime = WorkRuntime(database, journal, lambda: case[1][0])
    stream = journal.list_streams()[0]["stream"]["id"]
    original = journal.stream_snapshot(stream)
    assert archive(runtime, stream, reply_directory=directory)["archived"]
    capacity = journal.capacity()
    assert capacity["used_bytes"] == 0 and capacity["can_open_stream"]
    assert can_admit(capacity)
    traffic = requests.read_bytes()
    send(client, case)
    ready(case)
    with database.snapshot() as connection:
        runs = connection.execute("SELECT count(*) FROM runs").fetchone()[0]
        permits = connection.execute("SELECT count(*) FROM execution_permits").fetchone()[0]
    name, limit = (
        ("STORAGE_LIMIT", capacity["archived_bytes"])
        if boundary == "bytes"
        else ("MAX_SEGMENTS", capacity["archived_streams"])
    )
    with monkeypatch.context() as patch:
        patch.setattr(journal_segments, name, limit)
        # The journal is intact and has hot room; only its protected cold bound is reached.
        assert journal.capacity()["can_open_stream"]
        assert not can_admit(journal.capacity())
        assert journal.stream_snapshot(stream) == original
        report = check_reply_service(
            database,
            owner,
            profile,
            key,
            clock=lambda: case[1][0],
            native_home=journal.path.parent,
            model="gpt-6.2-sol",
        )
        assert "native_storage_capacity" in report["blockers"]
        assert "native_configuration_invalid" not in report["blockers"]
        assert tick(native_reply_case)["outcome"] == "native_storage_capacity"
        with pytest.raises(ProtocolError, match="retention capacity"):
            journal.open_stream("milo-work-" + uuid4().hex + ".service")
        with database.snapshot() as connection:
            assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == runs
            assert (
                connection.execute("SELECT count(*) FROM execution_permits").fetchone()[0]
                == permits
            )
    # Reaching the same bound during an existing stream must not prevent its final records.
    unit = "milo-work-" + uuid4().hex + ".service"
    active = journal.open_stream(unit)
    with monkeypatch.context() as patch:
        patch.setattr(journal_segments, name, limit)
        journal.require_turn_capacity(active)
        journal.record(active, {"kind": "process", "unit": unit, "phase": "stop_confirmed"})
        assert journal.close_stream(active, stop=lambda: True)
        assert journal.stream_snapshot(stream) == original
    assert requests.read_bytes() == traffic

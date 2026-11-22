import hashlib
import json
import sqlite3
from uuid import uuid4

import pytest
from test_codex_journal import bound as bound
from test_codex_journal import unit

from milo_home.codex_journal import BusinessBinding, PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.journal_archive import quarantine_orphans, seal
from milo_home.journal_storage import SEGMENTS, canonical


@pytest.fixture
def segment_case(bound):
    return prepare_stream(bound[0])


def prepare_stream(journal, index=1):
    linked = journal.business_binding() or BusinessBinding(
        *[str(uuid4()) for _ in range(4)], str(journal.path), 1, 1, journal.fingerprint()
    )
    journal.bind_business(linked)
    work = {name: str(uuid4()) for name in ("input_id", "permit_id", "window_id", "task_id")}
    work["stream_id"] = f"{index:032x}"
    name = unit(index)
    stream = work["stream_id"]
    journal.open_work_stream(
        linked, unit=name, stream=stream, window_id=work["window_id"], task_id=work["task_id"]
    )
    identity = {
        "unit": name,
        "group": "/system.slice/" + name,
        "boot_id": str(uuid4()),
        "invocation": "b" * 32,
        "main_pid": 42,
    }
    journal.record(stream, {"kind": "process", "phase": "ready", **identity})
    journal.record(
        stream,
        {
            "kind": "intent",
            "work": work,
            "request": {
                "id": 1,
                "method": "turn/start",
                "params": {
                    "threadId": journal.binding().thread_id,
                    "clientUserMessageId": work["input_id"],
                },
            },
        },
    )
    journal.record(
        stream,
        {
            "kind": "response",
            "id": 1,
            "response": {
                "id": 1,
                "result": {"turn": {"id": "turn", "status": "completed"}},
            },
        },
    )
    journal.record(
        stream,
        {
            "kind": "work_terminal",
            "work": work,
            "request_id": 1,
            "turn_id": "turn",
            "outcome": "completed",
            "observed_at": 100,
        },
    )
    stop = {"kind": "process", "phase": "stop_confirmed", "unit": name}
    stop_sequence = journal.record(stream, stop)
    assert journal.close_stream(stream, stop=lambda: True)
    settlement = {
        "eligible": True,
        "blocked_by": [],
        "business_digest": "c" * 64,
        "scope": {
            "kind": "work",
            "stream_id": stream,
            "unit": name,
            "binding_fingerprint": journal.fingerprint(),
            "process_identity": identity,
            "stop_sequence": stop_sequence,
            "stop": stop,
        },
    }
    return journal, stream, settlement


def archive(case):
    journal, stream, settlement = case
    expected = journal.stream_snapshot(stream)
    journal.enable_segmentation()
    return seal(journal, expected, settlement)


def test_explicit_migration_keeps_v2_read_only_and_original_binding(segment_case):
    journal, stream, _ = segment_case
    before = journal.binding_record(), journal.fingerprint(), journal.stream_snapshot(stream)
    assert journal.storage_summary() is None
    opened = PrivateJournal(journal.path.parent, initialize=False, read_only=True)
    assert opened.storage_summary() is None
    with opened._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    journal.enable_segmentation()
    before_storage = journal.storage_summary()
    journal.enable_segmentation()
    assert (
        journal.binding_record(),
        journal.fingerprint(),
        journal.stream_snapshot(stream),
    ) == before
    assert journal.storage_summary()["sequence_highwater"] == before[2]["entries"][-1][0]
    assert journal.storage_summary() == before_storage


def test_original_payload_and_highest_sequence_survive_cold_base_and_new_stream(segment_case):
    journal, stream, _ = segment_case
    expected = journal.stream_snapshot(stream)
    with journal._connect() as connection:
        original = [
            tuple(row)
            for row in connection.execute("SELECT sequence,payload FROM entries ORDER BY sequence")
        ]
    archive(segment_case)
    assert journal.stream_snapshot(stream) == expected
    with journal._connect() as connection:
        assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
    content = json.loads((journal.path.parent / SEGMENTS / (stream + ".json")).read_bytes())
    assert [tuple(row) for row in content["entries"]] == original
    assert journal.capacity()["used_bytes"] == 0 and journal.capacity()["archived_streams"] == 1
    following = journal.open_stream(unit(2))
    sequence = journal.record(
        following, {"kind": "notification", "message": {"method": "synthetic"}}
    )
    assert sequence > original[-1][0]
    assert len(journal.list_streams()) == 2
    assert journal.storage_summary()["entry_count"] == len(original) + 1


def test_closed_stream_accepts_fresh_hot_stop_without_rewriting_base(segment_case):
    journal, stream, settlement = segment_case
    archive(segment_case)
    path = journal.path.parent / SEGMENTS / (stream + ".json")
    original = path.read_bytes()
    stop = {"kind": "work_stop", **settlement["scope"]["process_identity"], "checked_at": 101}
    sequence = journal.record(stream, stop)
    assert path.read_bytes() == original
    assert journal.stream_snapshot(stream)["entries"][-1] == (sequence, stop)
    journal.reconcile_stream(stream, sequence)
    assert journal.segment_catalogue()[0]["has_overlay"]
    assert journal.capacity()["used_bytes"] == len(json.dumps(stop, ensure_ascii=False).encode())


@pytest.mark.parametrize("damage", ["missing", "changed", "extra", "sequence", "settlement"])
def test_missing_or_tampered_segment_never_becomes_empty_history(segment_case, damage):
    journal, stream, _ = segment_case
    archive(segment_case)
    path = journal.path.parent / SEGMENTS / (stream + ".json")
    if damage == "missing":
        path.unlink()
    elif damage == "changed":
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "extra":
        (path.parent / "orphan.json").write_bytes(b"{}")
    else:
        value = json.loads(path.read_bytes())
        if damage == "sequence":
            value["entries"][1][0] = value["entries"][0][0]
        else:
            value["settlement"]["scope"]["process_identity"]["main_pid"] += 1
        data = canonical(value)
        path.write_bytes(data)
        with journal._connect() as connection:
            connection.execute("DROP TRIGGER immutable_archive_update")
            connection.execute(
                "UPDATE stream_archives SET sha256=?,file_bytes=?,settlement_digest=?",
                (
                    hashlib.sha256(data).hexdigest(),
                    len(data),
                    hashlib.sha256(canonical(value["settlement"])).hexdigest(),
                ),
            )
    with pytest.raises((ProtocolError, OSError, ValueError)):
        journal.stream_snapshot(stream)


def test_failed_index_commit_retains_hot_evidence_and_orphan_can_be_isolated(
    segment_case, tmp_path
):
    journal, stream, settlement = segment_case
    expected = journal.stream_snapshot(stream)
    journal.enable_segmentation()
    with journal._connect() as connection:
        connection.execute(
            "CREATE TRIGGER synthetic_fail BEFORE INSERT ON stream_archives "
            "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        seal(journal, expected, settlement)
    with journal._connect() as connection:
        assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == len(
            expected["entries"]
        )
        assert connection.execute("SELECT count(*) FROM stream_archives").fetchone()[0] == 0
        connection.execute("DROP TRIGGER synthetic_fail")
    with pytest.raises(ProtocolError, match="unregistered"):
        journal.storage_summary()
    output = tmp_path.parent / ("isolated-" + uuid4().hex)
    report = quarantine_orphans(journal, output)
    assert report["original_bytes_retained"] and report["isolated"] == 1
    assert (output / "evidence" / (stream + ".json")).is_file()
    assert journal.stream_snapshot(stream) == expected
    assert seal(journal, expected, settlement)["archived"]


def test_failed_file_publication_retains_all_hot_evidence(segment_case, monkeypatch, tmp_path):
    journal, stream, settlement = segment_case
    expected = journal.stream_snapshot(stream)
    journal.enable_segmentation()

    def fail_link(*args, **kwargs):
        raise OSError("Synthetic interrupted segment publication")

    monkeypatch.setattr("milo_home.journal_archive.os.link", fail_link)
    with pytest.raises(OSError, match="interrupted"):
        seal(journal, expected, settlement)
    with journal._connect() as connection:
        assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == len(
            expected["entries"]
        )
        assert connection.execute("SELECT count(*) FROM stream_archives").fetchone()[0] == 0
    output = tmp_path.parent / ("isolated-staging-" + uuid4().hex)
    assert quarantine_orphans(journal, output)["isolated"] == 1
    assert journal.stream_snapshot(stream) == expected


def test_highwater_damage_refuses_append_without_reusing_archived_identity(segment_case):
    journal, stream, _ = segment_case
    archive(segment_case)
    with journal._connect() as connection:
        connection.execute("UPDATE sequence_highwater SET value=0")
    with pytest.raises(ProtocolError, match="highwater"):
        journal.open_stream(unit(2))


def test_maximum_sequence_is_readable_but_cannot_be_allocated_again(segment_case):
    journal, _, _ = segment_case
    archive(segment_case)
    following = journal.open_stream(unit(2))
    with journal._connect() as connection:
        connection.execute("UPDATE sequence_highwater SET value=?", (2**63 - 1,))
    before = journal.storage_summary()
    with pytest.raises(ProtocolError, match="exhausted"):
        journal.record(following, {"kind": "notification", "message": {"method": "synthetic"}})
    assert journal.storage_summary() == before


def test_segment_budget_rejects_before_reading_payloads(segment_case, monkeypatch):
    journal, _, _ = segment_case
    archive(segment_case)
    reads = []

    def forbidden_read(*args, **kwargs):
        reads.append(args)
        raise AssertionError("Budget must be checked before segment bytes are read")

    monkeypatch.setattr("milo_home.journal_segments.private_read", forbidden_read)
    with pytest.raises(ProtocolError, match="budget"):
        journal.storage_summary(budget=1)
    assert reads == []

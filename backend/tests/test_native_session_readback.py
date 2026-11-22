import json
import sqlite3
from pathlib import Path

import pytest
from test_native_session_backup import native_case as native_case

from milo_home.codex_rpc import ProtocolError
from milo_home.native_session_backup import capture_native, rehearse_native
from milo_home.native_session_readback import readback_native, verify_history
from milo_home.native_session_relocation import relocate_rehearsal, rollout_facts


@pytest.fixture
def readback_case(native_case):
    root, home, thread, connection = native_case
    connection.execute("ALTER TABLE threads ADD COLUMN rollout_path TEXT")
    connection.execute("ALTER TABLE threads ADD COLUMN model_provider TEXT")
    connection.execute("ALTER TABLE threads ADD COLUMN model TEXT")
    connection.execute("CREATE TABLE rollout_migration_skipped_rollouts(rollout_path TEXT)")
    path = home / "sessions/2026/10/19/rollout.jsonl"
    connection.execute(
        "UPDATE threads SET rollout_path=?, model_provider=?, model=?",
        (str(path), "synthetic-provider", "synthetic-model"),
    )
    connection.commit()
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "turn_context", "payload": {"turn_id": "turn-1"}}) + "\n")
    report = capture_native(home, root / "archive", thread, writers_stopped=True)
    manifest = json.loads((root / "archive/manifest.json").read_bytes())
    rehearse_native(root / "archive", root / "restored", report["manifest_sha256"])
    return root, home, thread, connection, manifest, report


@pytest.mark.parametrize("state_filename", ["state_5.sqlite", "state_42.sqlite"])
def test_relocation_only_changes_new_index_and_creates_synthetic_configuration(
    readback_case, state_filename
):
    root, home, thread, source, manifest, _ = readback_case
    target = root / "restored" / state_filename
    if state_filename != "state_5.sqlite":
        (root / "restored/state_5.sqlite").rename(target)
    original = source.execute("SELECT rollout_path FROM threads").fetchone()[0]
    chosen, config, count = relocate_rehearsal(root / "restored", manifest)
    assert count == 1 and chosen["turn_ids"] == ["turn-1"]
    assert chosen["path"] == str(root / "restored/sessions/2026/10/19/rollout.jsonl")
    with sqlite3.connect(target) as restored:
        assert restored.execute("SELECT id,rollout_path FROM threads").fetchone() == (
            thread,
            chosen["path"],
        )
    assert source.execute("SELECT rollout_path FROM threads").fetchone() == (original,)
    assert str(home) in original
    assert b"https://synthetic.invalid" in config.content
    assert not (root / "restored/auth.json").exists()


@pytest.mark.parametrize("fault", ["ambiguous", "missing-columns", "view"])
def test_relocation_requires_one_index_with_a_compatible_schema(readback_case, fault):
    root, _, _, _, manifest, _ = readback_case
    restored = root / "restored"
    if fault == "ambiguous":
        (restored / "state_42.sqlite").touch()
    else:
        with sqlite3.connect(restored / "state_5.sqlite") as connection:
            if fault == "view":
                connection.execute("ALTER TABLE threads RENAME TO alternate")
                connection.execute("CREATE VIEW threads AS SELECT * FROM alternate")
            else:
                connection.execute("ALTER TABLE threads DROP COLUMN rollout_path")
    with pytest.raises(ValueError, match="unambiguous state index|rollout fields|rollout tables"):
        relocate_rehearsal(restored, manifest)
    assert not (restored / "config.toml").exists()


def test_relocation_rejects_two_captured_rollouts_for_one_thread(readback_case):
    root, home, _, _, manifest, _ = readback_case
    restored = root / "restored"
    original = "sessions/2026/10/19/rollout.jsonl"
    alternate = "sessions/2026/10/19/second.jsonl"
    (restored / alternate).write_bytes((restored / original).read_bytes())
    manifest["artifacts"][alternate] = manifest["artifacts"][original]
    database = restored / "state_42.sqlite"
    (restored / "state_5.sqlite").rename(database)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE threads RENAME TO original")
        connection.execute("CREATE TABLE threads AS SELECT * FROM original")
        connection.execute(
            "INSERT INTO threads SELECT id, title, ?, model_provider, model FROM original",
            (str(home / alternate),),
        )
        connection.execute("DROP TABLE original")
    before = database.read_bytes()
    with pytest.raises(ValueError, match="unique canonical thread IDs"):
        relocate_rehearsal(restored, manifest)
    assert database.read_bytes() == before
    assert not (restored / "config.toml").exists()


@pytest.mark.parametrize("fault", ["outside", "missing", "wrong-thread", "skipped", "no-turns"])
def test_unsupported_or_inconsistent_native_index_never_gets_partially_relocated(
    readback_case, fault
):
    root, _, _, _, manifest, _ = readback_case
    database = root / "restored/state_5.sqlite"
    with sqlite3.connect(database) as connection:
        if fault == "outside":
            connection.execute("UPDATE threads SET rollout_path='/outside/rollout.jsonl'")
        elif fault == "missing":
            connection.execute("UPDATE threads SET rollout_path=rollout_path || '.missing'")
        elif fault == "wrong-thread":
            connection.execute("UPDATE threads SET id='incorrect-thread'")
        elif fault == "skipped":
            connection.execute("INSERT INTO rollout_migration_skipped_rollouts VALUES ('unknown')")
        elif fault == "no-turns":
            rollout = root / "restored/sessions/2026/10/19/rollout.jsonl"
            rollout.write_bytes(rollout.read_bytes().splitlines()[0] + b"\n")
        before = connection.execute("SELECT id,rollout_path FROM threads").fetchall()
    with pytest.raises(ValueError):
        relocate_rehearsal(root / "restored", manifest)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id,rollout_path FROM threads").fetchall() == before
    assert not (root / "restored/config.toml").exists()


class ReadPeer:
    def __init__(self, chosen, workspace, fault):
        self.chosen = chosen
        self.workspace = workspace
        self.fault = fault
        self.methods = []

    def notify_initialized(self):
        pass

    def request(self, method, params):
        self.methods.append(method)
        if method == "initialize":
            return {}
        thread = {
            "id": self.chosen["thread_id"],
            "path": self.chosen["path"],
            "turns": [{"id": "turn-1"}],
            "status": {"type": "idle"},
        }
        if self.fault == "old-path":
            thread["path"] = "/original/home/sessions/rollout.jsonl"
        if self.fault == "wrong-turns":
            thread["turns"] = [{"id": "different-turn"}]
        if self.fault == "wrong-thread":
            thread["id"] = "incorrect-thread"
        if method == "thread/read":
            return {"thread": thread}
        return {
            "thread": thread,
            "modelProvider": "fallback" if self.fault == "fallback" else "provider",
            "model": "model",
            "cwd": str(self.workspace),
            "approvalPolicy": "never",
            "sandbox": {"type": "dangerFullAccess"},
        }


@pytest.mark.parametrize("fault", [None, "old-path", "wrong-turns", "wrong-thread", "fallback"])
def test_protocol_rejects_original_path_incomplete_history_and_provider_fallback(tmp_path, fault):
    chosen = {
        "thread_id": "thread",
        "path": str(tmp_path / "rollout.jsonl"),
        "turn_ids": ["turn-1"],
        "provider": "provider",
        "model": "model",
    }
    peer = ReadPeer(chosen, tmp_path, fault)
    if fault:
        with pytest.raises(ProtocolError):
            verify_history(peer, chosen, tmp_path)
    else:
        assert verify_history(peer, chosen, tmp_path) == 1
    assert set(peer.methods) <= {"initialize", "thread/read", "thread/resume"}


def test_nested_output_refused_before_native_launch_or_archive_mutation(readback_case):
    root, _, _, _, _, report = readback_case

    class UnavailableInstallation:
        def verify(self):
            pytest.fail("Installation must not be touched for an invalid output")

    with pytest.raises(ValueError, match="disjoint"):
        readback_native(
            root / "archive",
            root / "archive/new",
            report["manifest_sha256"],
            UnavailableInstallation(),
        )
    assert not (root / "archive/new").exists()


@pytest.mark.parametrize("seconds", [0, 4, 31, True])
def test_readback_requires_finite_small_window_without_opening_any_data(seconds):
    with pytest.raises(ValueError, match="5–30"):
        readback_native(Path("/absent"), Path("/unused"), "bad", None, seconds=seconds)


@pytest.mark.parametrize("marker_started", [False, True])
def test_native_compaction_context_is_not_a_missing_historical_turn(tmp_path, marker_started):
    records = [
        {"type": "session_meta", "payload": {"id": "thread"}},
        {"type": "turn_context", "payload": {"turn_id": "auto-compact-0"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"type": "turn_context", "payload": {"turn_id": "turn-1"}},
        {"type": "turn_context", "payload": {"turn_id": "older-context-only-turn"}},
    ]
    if marker_started:
        records.append(
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "auto-compact-0"}}
        )
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    path.chmod(0o600)
    expected = ["turn-1", "older-context-only-turn"]
    if marker_started:
        expected.insert(0, "auto-compact-0")
    assert rollout_facts(tmp_path, path.name) == ("thread", expected)

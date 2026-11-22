import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from uuid import uuid4

import pytest

from milo_home.native_session_backup import capture_native, rehearse_native
from milo_home.native_session_files import inventory
from milo_home.workspace_archive_files import DEFAULT_BUDGET

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Private native backup requires root")


@pytest.fixture
def native_case(private_runtime_directory):
    root = private_runtime_directory("milo-native-backup-")
    home = root / "native"
    home.mkdir(mode=0o700)
    thread = str(uuid4())
    sessions = home / "sessions/2026/10/19"
    sessions.mkdir(parents=True)
    (sessions / "rollout.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": thread}})
        + '\n{"type":"synthetic","text":"private conversation"}\n'
    )
    (home / "installation_id").write_text("synthetic-installation")
    (home / "auth.json").write_text("SYNTHETIC_AUTH_MUST_BE_OMITTED")
    (home / "config.toml").write_text("SYNTHETIC_CONFIG_MUST_BE_OMITTED")
    connection = sqlite3.connect(home / "state_5.sqlite")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT)")
    connection.execute("INSERT INTO threads VALUES (?, 'Synthetic WAL record')", (thread,))
    connection.commit()
    yield root, home, thread, connection
    connection.close()


def capture(case, **options):
    root, home, thread, _ = case
    return capture_native(home, root / "archive", thread, writers_stopped=True, **options)


def test_wal_mode_without_sidecars_leaves_source_untouched(native_case):
    root, home, _, _ = native_case
    source = home / "logs_2.sqlite"
    with closing(sqlite3.connect(source)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("CREATE TABLE events (value TEXT)")
        connection.execute("INSERT INTO events VALUES ('checkpointed')")
        connection.commit()

    assert sorted(home.glob("logs_2.sqlite*")) == [source]
    before = source.read_bytes()
    capture(native_case)
    assert sorted(home.glob("logs_2.sqlite*")) == [source]
    assert source.read_bytes() == before
    with closing(sqlite3.connect(root / "archive/state/logs_2.sqlite")) as restored:
        assert restored.execute("SELECT value FROM events").fetchone() == ("checkpointed",)
        assert restored.execute("PRAGMA journal_mode").fetchone() == ("delete",)


def test_actual_cli_preserves_wal_records_and_omits_authentication(native_case):
    root, home, thread, connection = native_case
    before = inventory(home, (), DEFAULT_BUDGET)
    environment = {**os.environ, "MILO_DATABASE": "/must-not-open"}
    command = [
        "milo-home",
        "backup-native-session",
        "capture",
        "--home",
        str(home),
        "--thread",
        thread,
        "--out",
        str(root / "archive"),
        "--writers-stopped",
    ]
    process = subprocess.run(command, capture_output=True, text=True, timeout=15, env=environment)
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["protocol_restore_verified"] is False
    process = subprocess.run(
        [
            "milo-home",
            "backup-native-session",
            "rehearse",
            "--archive",
            str(root / "archive"),
            "--out",
            str(root / "restored"),
            "--manifest-sha256",
            report["manifest_sha256"],
        ],
        capture_output=True,
        text=True,
        timeout=15,
        env=environment,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["automatic_resume"] is False
    assert inventory(home, (), DEFAULT_BUDGET) == before
    assert connection.execute("SELECT title FROM threads").fetchone() == ("Synthetic WAL record",)
    with sqlite3.connect(root / "restored/state_5.sqlite") as restored:
        assert restored.execute("SELECT id, title FROM threads").fetchone() == (
            thread,
            "Synthetic WAL record",
        )
    for name in ("auth.json", "config.toml", "state_5.sqlite-wal", "state_5.sqlite-shm"):
        assert not (root / "restored" / name).exists()
    assert not (root / "restored/INCOMPLETE").exists()
    assert "private conversation" not in process.stdout
    for artifact in (root / "archive").rglob("*"):
        if artifact.is_file():
            assert artifact.stat().st_mode & 0o777 == 0o600
            assert b"MUST_BE_OMITTED" not in artifact.read_bytes()
        else:
            assert artifact.stat().st_mode & 0o777 == 0o700
    for directory in (root / "restored").rglob("*"):
        if directory.is_dir():
            assert directory.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("path", ["new-state.bin", "new-state.sqlite"])
def test_unknown_native_files_require_classification(native_case, path):
    root, home, _, _ = native_case
    (home / path).write_bytes(b"synthetic unknown state")
    with pytest.raises(ValueError, match="Unclassified"):
        capture(native_case)
    assert not (root / "archive").exists()
    capture(native_case, exclude=[path])


@pytest.mark.parametrize(
    "path", ["sessions", "state_5.sqlite", "state_5.sqlite-wal", "installation_id"]
)
def test_core_state_cannot_be_excluded(native_case, path):
    with pytest.raises(ValueError, match="cannot be excluded"):
        capture(native_case, exclude=[path])


def test_explicit_cache_exclusion_and_archived_rollout_preserved(native_case):
    root, home, _, _ = native_case
    (home / "skills").mkdir()
    (home / "skills/tool.py").write_text("raise RuntimeError('must never execute')")
    archived = home / "archived_sessions"
    archived.mkdir()
    (archived / "old.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": str(uuid4())},
            }
        )
        + "\n"
    )
    report = capture(native_case, exclude=["skills"])
    rehearse_native(root / "archive", root / "restored", report["manifest_sha256"])
    assert not (root / "restored/skills").exists()
    assert (root / "restored/archived_sessions/old.jsonl").read_bytes() == (
        archived / "old.jsonl"
    ).read_bytes()


def test_missing_stop_confirmation_or_wrong_thread_rejected(native_case):
    root, home, thread, _ = native_case
    with pytest.raises(ValueError, match="stopped-writer"):
        capture_native(home, root / "archive", thread)
    with pytest.raises(ValueError, match="no native rollout"):
        capture_native(home, root / "archive", uuid4(), writers_stopped=True)


def test_default_and_active_home_are_rejected(native_case, monkeypatch):
    _, home, _, _ = native_case
    monkeypatch.setenv("CODEX_HOME", str(home))
    with pytest.raises(ValueError, match="dedicated"):
        capture(native_case)


def test_linked_native_state_rejected(native_case):
    _, home, _, _ = native_case
    (home / "session_index.jsonl").symlink_to(home / "installation_id")
    with pytest.raises(ValueError, match="links"):
        capture(native_case)


def test_source_mutation_retains_incomplete_marker(native_case, monkeypatch):
    import milo_home.native_session_backup as module

    root, home, _, _ = native_case
    original = module.sqlite_snapshot

    def mutate(*args):
        original(*args)
        (home / "installation_id").write_text("changed")

    monkeypatch.setattr(module, "sqlite_snapshot", mutate)
    with pytest.raises(ValueError, match="changed"):
        capture(native_case)
    assert (root / "archive/INCOMPLETE").exists()


@pytest.mark.parametrize("manifest", [False, True])
def test_tampering_rejected_before_restore_directory_created(native_case, manifest):
    root, _, _, _ = native_case
    report = capture(native_case)
    path = root / "archive" / ("manifest.json" if manifest else "state/installation_id")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="differs"):
        rehearse_native(root / "archive", root / "restored", report["manifest_sha256"])
    assert not (root / "restored").exists()


def test_manifest_path_escape_rejected_even_with_new_trusted_digest(native_case):
    root, _, _, _ = native_case
    capture(native_case)
    path = root / "archive/manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["artifacts"]["../outside"] = manifest["artifacts"]["installation_id"]
    content = json.dumps(manifest).encode()
    path.write_bytes(content)
    with pytest.raises(ValueError, match="canonical"):
        rehearse_native(root / "archive", root / "restored", hashlib.sha256(content).hexdigest())


def test_existing_or_nested_output_rejected(native_case):
    root, home, thread, _ = native_case
    for output in (home, home / "archive", root):
        with pytest.raises(ValueError):
            capture_native(home, output, thread, writers_stopped=True)


def test_capacity_failure_leaves_source_unchanged(native_case):
    root, home, _, _ = native_case
    before = inventory(home, (), DEFAULT_BUDGET)
    with pytest.raises(ValueError, match="budget"):
        capture(native_case, budget=1)
    assert inventory(home, (), DEFAULT_BUDGET) == before
    assert not (root / "archive").exists()


@pytest.mark.parametrize("metadata", ["[]", "null", "invalid", '{"type":"other"}'])
def test_invalid_rollout_metadata_rejected_before_capture(native_case, metadata):
    root, home, _, _ = native_case
    (home / "sessions/2026/10/19/rollout.jsonl").write_text(metadata + "\n")
    with pytest.raises(ValueError, match="metadata"):
        capture(native_case)
    assert not (root / "archive").exists()


def test_insufficient_free_space_rejected_without_creating_archive(native_case, monkeypatch):
    import milo_home.workspace_archive_files as files

    root, _, _, _ = native_case
    usage = files.shutil.disk_usage(root)
    monkeypatch.setattr(files.shutil, "disk_usage", lambda path: usage._replace(free=0))
    with pytest.raises(ValueError, match="disk space"):
        capture(native_case)
    assert not (root / "archive").exists()

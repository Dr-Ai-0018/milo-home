"""Offline private journal and original creation evidence; never rebind or execute."""

import hashlib
import json
import re
import stat
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from .codex_journal import PrivateJournal
from .database_identity import private_run_directory
from .journal_storage import SEGMENTS
from .native_session_backup import independent_output
from .native_session_files import (
    DATABASE,
    METADATA,
    OMITTED,
    RECORD_ROOTS,
    SQLITE_MAINTENANCE_LOCK,
    make_private_parents,
    sqlite_snapshot,
    verify_sqlite_maintenance_lock,
)
from .runtime_native_evidence import (
    NATIVE_DATABASES,
    NATIVE_FILES,
    native_evidence_name,
    native_evidence_summary,
)
from .work_attempt_history import HISTORY, MAX_HISTORY
from .workspace_archive_files import (
    DEFAULT_BUDGET,
    complete,
    credential_path,
    json_bytes,
    new_directory,
    read_file,
    write_file,
)

JOURNAL = "session-journal.sqlite3"
CLAIMS = {"main-registration.json", "main-creation.json"}
EVENTS = "main-creation-events"
_EVENT_FILE = re.compile(r"[0-9]{6}\.json")
_NATIVE_RUNTIME_DIRECTORIES = {"shell_snapshots", "skills", "thread-writer-locks", "tmp"}


def _attempt_id(value):
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def evidence_name(name):
    parts = name.split("/")
    if name == JOURNAL or name in CLAIMS or native_evidence_name(name):
        return True
    if len(parts) == 2 and parts[0] == SEGMENTS:
        return re.fullmatch(r"[0-9a-f]{32}\.json", parts[1]) is not None
    if len(parts) == 2 and parts[0] == EVENTS:
        return _EVENT_FILE.fullmatch(parts[1]) is not None
    if len(parts) not in {3, 4} or parts[0] != HISTORY or not _attempt_id(parts[1]):
        return False
    if len(parts) == 3:
        return parts[2] == "main-creation.json"
    return parts[2] == EVENTS and _EVENT_FILE.fullmatch(parts[3]) is not None


def _private_evidence_dir(path):
    if path.resolve() != path or not path.is_dir():
        raise ValueError("Creation evidence directory must not be linked")
    metadata = path.stat()
    if metadata.st_uid != 0 or metadata.st_mode & 0o077:
        raise ValueError("Creation evidence directory must be private")


def _event_entries(path):
    _private_evidence_dir(path)
    events = sorted(path.iterdir())
    if len(events) > 128 or [p.name for p in events] != [
        f"{number:06d}.json" for number in range(1, len(events) + 1)
    ]:
        raise ValueError("Creation evidence sequence is invalid")
    return events


def _history_entries(path):
    _private_evidence_dir(path)
    history = sorted(path.iterdir())
    if not 1 <= len(history) <= MAX_HISTORY:
        raise ValueError("Creation history exceeds its attempt bound or is empty")
    for attempt in history:
        _private_evidence_dir(attempt)
        if not _attempt_id(attempt.name) or {p.name for p in attempt.iterdir()} != {
            "main-creation.json",
            EVENTS,
        }:
            raise ValueError("Unclassified original creation history")
        events = _event_entries(attempt / EVENTS)
        if not events:
            raise ValueError("Historical creation event sequence is empty")
        yield attempt / "main-creation.json"
        yield from events


def runtime_inventory(home, budget):
    private_run_directory(home)
    if not (home / JOURNAL).exists() and not (home / JOURNAL).is_symlink():
        raise ValueError("Existing runtime journal is required")
    files = {}
    omitted = {}
    total = 0
    journal = PrivateJournal(home, initialize=False, read_only=True)
    storage = journal.storage_summary(budget=budget)
    paths = sorted(home.iterdir())
    if len(paths) > 20000:
        raise ValueError("Runtime inventory exceeds the file count bound")
    for path in paths:
        name = path.name
        if path.parent == home and name == SQLITE_MAINTENANCE_LOCK:
            verify_sqlite_maintenance_lock(path)
            omitted[name] = "native-sqlite-maintenance-lock"
            continue
        if name in OMITTED or credential_path(name):
            omitted[name] = "configuration-or-credential"
            continue
        if path.parent == home and name in _NATIVE_RUNTIME_DIRECTORIES:
            metadata = path.lstat()
            if (
                path.resolve() != path
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_mode & 0o022
            ):
                raise ValueError("Native auxiliary directory must be root-owned and non-linked")
            # The native collector still inventories the contents and refuses
            # unknown files unless the operator names an exact exclusion.
            omitted[name] = "separate-native-session-archive-required"
            continue
        if name in RECORD_ROOTS or name in METADATA or DATABASE.fullmatch(name):
            omitted[name] = "separate-native-session-archive-required"
            continue
        if name.endswith(("-wal", "-shm")) and DATABASE.fullmatch(name[:-4]):
            omitted[name] = "separate-native-session-archive-required"
            continue
        if path.parent == home and name == EVENTS:
            paths.extend(_event_entries(path))
            continue
        if path.parent == home and name == HISTORY:
            paths.extend(_history_entries(path))
            continue
        if path.parent == home and name == SEGMENTS:
            if storage is None:
                raise ValueError("Unregistered journal segment directory")
            paths.extend(home / record["name"] for record in storage["archives"])
            continue
        name = path.relative_to(home).as_posix()
        databases = {JOURNAL} | NATIVE_DATABASES
        if name.endswith(("-wal", "-shm")) and name[:-4] in databases:
            if not (home / name[:-4]).is_file():
                raise ValueError("Runtime SQLite sidecar has no database")
        if name.endswith("-shm") and name[:-4] in databases:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_mode & 0o077
            ):
                raise ValueError("Runtime SQLite shared memory must be root-owned and private")
            continue
        if not evidence_name(name) and not (name.endswith("-wal") and name[:-4] in databases):
            raise ValueError("Unclassified private runtime evidence")
        metadata = path.lstat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o077:
            raise ValueError("Runtime evidence must be root-owned and private")
        content, fingerprint = read_file(home, name, budget - total)
        if name in CLAIMS or name.startswith((EVENTS + "/", HISTORY + "/")):
            if not isinstance(json.loads(content), dict):
                raise ValueError("Original runtime evidence must be a JSON object")
        total += len(content)
        files[name] = fingerprint
    if JOURNAL not in files:
        raise ValueError("Existing runtime journal is required")
    result = {"files": files, "omitted": omitted, "bytes": total}
    if storage is not None:
        result["storage"] = storage
    return result


def journal_summary(home):
    journal = PrivateJournal(home, initialize=False, read_only=True)
    binding = journal.business_binding()
    return {
        "fingerprint": journal.fingerprint(),
        "business_binding": asdict(binding) if binding else None,
        "outstanding": journal.outstanding(),
    }


def capture_runtime(home, output, *, writers_stopped=False, budget=DEFAULT_BUDGET):
    if not writers_stopped:
        raise ValueError("Runtime capture requires explicit stopped-writer confirmation")
    independent_output(home, output)
    before = runtime_inventory(home, budget)
    identity = [home.stat().st_dev, home.stat().st_ino]
    summary = journal_summary(home)
    journal = PrivateJournal(home, initialize=False, read_only=True)
    native_summary = native_evidence_summary(home, before["files"], journal, budget)
    new_directory(output, budget, before["bytes"] * 4 + 1024 * 1024)
    artifacts = {}
    total = 0
    for name in before["files"]:
        if name.endswith("-wal"):
            continue
        make_private_parents(output, "evidence/" + name)
        target = output / "evidence" / name
        if name in {JOURNAL} | NATIVE_DATABASES:
            sqlite_snapshot(home / name, target, budget - total, before["files"])
        else:
            content, _ = read_file(home, name, budget - total)
            write_file(target, content)
        _, artifacts[name] = read_file(output, "evidence/" + name, budget - total)
        total += artifacts[name]["bytes"]
    if (
        runtime_inventory(home, budget) != before
        or identity != [home.stat().st_dev, home.stat().st_ino]
        or journal_summary(home) != summary
        or journal_summary(output / "evidence") != summary
        or native_evidence_summary(
            output / "evidence",
            artifacts,
            PrivateJournal(output / "evidence", initialize=False, read_only=True),
            budget,
        )
        != native_summary
    ):
        raise ValueError("Runtime evidence changed during capture")
    manifest = {
        "version": 3 if "storage" in before else (2 if native_summary is not None else 1),
        "kind": "offline-private-runtime-evidence",
        "source": str(home),
        "source_identity": identity,
        "inventory": before,
        "journal": summary,
        "artifacts": artifacts,
        "writers_stopped_confirmed": True,
        "quiescence_verified": False,
        "automatic_resume": False,
    }
    if "storage" in before:
        manifest["storage"] = before["storage"]
        manifest["native_evidence"] = native_summary
    elif native_summary is not None:
        manifest["native_evidence"] = native_summary
    content = json_bytes(manifest)
    if total + len(content) > budget:
        raise ValueError("Runtime archive exceeds its byte budget")
    write_file(output / "manifest.json", content)
    complete(output)
    return {"captured": True, "manifest_sha256": hashlib.sha256(content).hexdigest()}


def verified_runtime(archive, digest, budget):
    private_run_directory(archive)
    if (archive / "INCOMPLETE").exists():
        raise ValueError("Runtime archive is incomplete")
    content, _ = read_file(archive, "manifest.json", budget)
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Runtime manifest differs from the trusted digest")
    manifest = json.loads(content)
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") not in {1, 2, 3}
        or manifest.get("kind") != "offline-private-runtime-evidence"
        or not isinstance(manifest.get("artifacts"), dict)
        or JOURNAL not in manifest["artifacts"]
    ):
        raise ValueError("Unsupported private runtime archive")
    evidence = archive / "evidence"
    history = evidence / HISTORY
    expected_history = {name for name in manifest["artifacts"] if name.startswith(HISTORY + "/")}
    actual_history = (
        {path.relative_to(evidence).as_posix() for path in _history_entries(history)}
        if history.exists() or history.is_symlink()
        else set()
    )
    if actual_history != expected_history:
        raise ValueError("Creation history differs from the runtime archive manifest")
    total = len(content)
    for name, expected in manifest["artifacts"].items():
        if not evidence_name(name):
            raise ValueError("Unclassified runtime archive artifact")
        _, actual = read_file(archive, "evidence/" + name, budget)
        total += actual["bytes"]
        if actual != expected or actual["mode"] != 0o600 or total > budget:
            raise ValueError("Runtime artifact differs from manifest or budget")
    native_summary = native_evidence_summary(
        evidence,
        manifest["artifacts"],
        PrivateJournal(evidence, initialize=False, read_only=True),
        budget,
    )
    storage = PrivateJournal(evidence, initialize=False, read_only=True).storage_summary(
        budget=budget
    )
    if (
        manifest["version"] == 3
        and (
            storage is None
            or storage != manifest.get("storage")
            or "native_evidence" not in manifest
            or native_summary != manifest["native_evidence"]
            or journal_summary(evidence) != manifest.get("journal")
        )
    ) or (manifest["version"] < 3 and storage is not None):
        raise ValueError("Journal storage differs from its runtime archive version or summary")
    if (manifest["version"] == 1 and (set(manifest["artifacts"]) & NATIVE_FILES)) or (
        manifest["version"] == 2 and native_summary != manifest.get("native_evidence")
    ):
        raise ValueError("Native runtime evidence differs from its archive summary")
    if manifest["version"] >= 2:
        expected = {name for name in manifest["inventory"]["files"] if not name.endswith("-wal")}
        if (
            set(manifest["artifacts"]) != expected
            or runtime_inventory(evidence, budget)["files"] != manifest["artifacts"]
        ):
            raise ValueError("Native runtime archive omitted or added evidence")
    return manifest


def rehearse_runtime(archive, output, digest, *, budget=DEFAULT_BUDGET):
    manifest = verified_runtime(archive, digest, budget)
    independent_output(archive, output)
    independent_output(Path(manifest["source"]), output)
    new_directory(output, budget, sum(x["bytes"] for x in manifest["artifacts"].values()) * 2)
    for name, expected in manifest["artifacts"].items():
        content, _ = read_file(archive, "evidence/" + name, budget)
        make_private_parents(output, name)
        write_file(output / name, content)
        _, actual = read_file(output, name, budget)
        if actual != expected:
            raise ValueError("Restored runtime evidence differs from archive")
    if journal_summary(output) != manifest["journal"]:
        raise ValueError("Restored runtime journal association differs")
    verified_runtime(archive, digest, budget)
    complete(output)
    return {
        "restore_verified": True,
        "automatic_resume": False,
        "configuration_required": True,
        "original_binding_retained": True,
    }

"""Offline native session artifacts, independent of model and business execution."""

import hashlib
import json
from uuid import UUID

from .database_identity import private_run_directory
from .native_session_files import (
    file_kind,
    inventory,
    make_private_parents,
    sqlite_snapshot,
    verify_sqlite,
)
from .workspace_archive_files import (
    DEFAULT_BUDGET,
    complete,
    json_bytes,
    new_directory,
    read_file,
    write_file,
)


def independent_output(home, output):
    if output.is_relative_to(home) or home.is_relative_to(output):
        raise ValueError("Native backup requires disjoint source and output directories")


def capture_native(
    home, output, thread_id, *, writers_stopped=False, exclude=(), budget=DEFAULT_BUDGET
):
    if not writers_stopped:
        raise ValueError("Native capture requires explicit stopped-writer confirmation")
    thread_id = str(UUID(str(thread_id)))
    independent_output(home, output)
    before = inventory(home, exclude, budget)
    identity = [home.stat().st_dev, home.stat().st_ino]
    records = [name for name in before["files"] if name.startswith("sessions/")]
    records += [name for name in before["files"] if name.startswith("archived_sessions/")]
    matching = False
    for name in records:
        content, _ = read_file(home, name, budget)
        try:
            first = json.loads(content.split(b"\n", 1)[0])
        except (ValueError, IndexError) as error:
            raise ValueError("Native rollout lacks valid session metadata") from error
        if (
            not isinstance(first, dict)
            or first.get("type") != "session_meta"
            or not isinstance(first.get("payload"), dict)
        ):
            raise ValueError("Native rollout lacks valid session metadata")
        matching |= first["payload"].get("id") == thread_id
    if not matching:
        raise ValueError("Selected thread has no native rollout metadata")
    # Include DB expansion, archive, and verification overhead in the space gate.
    required = before["bytes"] * 4 + 1024 * 1024
    new_directory(output, budget, required)
    artifacts = {}
    total = 0
    for name, info in before["files"].items():
        if info["kind"] == "wal":
            continue
        destination = output / "state" / name
        make_private_parents(output, "state/" + name)
        if info["kind"] == "sqlite":
            sqlite_snapshot(home / name, destination, budget - total, before["files"])
        else:
            content, _ = read_file(home, name, budget - total)
            write_file(destination, content)
        _, actual = read_file(output, "state/" + name, budget - total)
        artifacts[name] = actual
        total += actual["bytes"]
    after = inventory(home, exclude, budget)
    if before != after or identity != [home.stat().st_dev, home.stat().st_ino]:
        raise ValueError("Native source changed during capture; archive remains incomplete")
    manifest = {
        "version": 1,
        "kind": "offline-native-session-state",
        "thread_id": thread_id,
        "source": str(home),
        "source_identity": identity,
        "inventory": before,
        "artifacts": artifacts,
        "writers_stopped_confirmed": True,
        "authentication_files_included": False,
        "configuration_included": False,
        "protocol_restore_verified": False,
    }
    content = json_bytes(manifest)
    if total + len(content) > budget:
        raise ValueError("Native archive exceeds its byte budget")
    write_file(output / "manifest.json", content)
    complete(output)
    return {
        "captured": True,
        "restore_verified": False,
        "protocol_restore_verified": False,
        "manifest_sha256": hashlib.sha256(content).hexdigest(),
    }


def verified_manifest(archive, digest, budget):
    private_run_directory(archive)
    if (archive / "INCOMPLETE").exists():
        raise ValueError("Native capture is incomplete")
    content, _ = read_file(archive, "manifest.json", budget)
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Native manifest differs from the trusted digest")
    manifest = json.loads(content)
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != 1
        or manifest.get("kind") != "offline-native-session-state"
    ):
        raise ValueError("Unsupported native archive")
    if not isinstance(manifest.get("artifacts"), dict) or not manifest["artifacts"]:
        raise ValueError("Native archive has no artifacts")
    total = len(content)
    for name, expected in manifest["artifacts"].items():
        file_kind(name)
        _, actual = read_file(archive, "state/" + name, budget)
        total += actual["bytes"]
        if actual != expected or actual["mode"] != 0o600 or total > budget:
            raise ValueError("Native artifact differs from the trusted manifest or budget")
    return manifest


def rehearse_native(archive, output, digest, *, budget=DEFAULT_BUDGET):
    from pathlib import Path

    manifest = verified_manifest(archive, digest, budget)
    independent_output(archive, output)
    independent_output(Path(manifest["source"]), output)
    required = sum(item["bytes"] for item in manifest["artifacts"].values()) * 2
    new_directory(output, budget, required)
    for name, expected in manifest["artifacts"].items():
        content, _ = read_file(archive, "state/" + name, budget)
        destination = output / name
        make_private_parents(output, name)
        write_file(destination, content)
        if file_kind(name) == "sqlite":
            verify_sqlite(destination)
        _, actual = read_file(output, name, budget)
        if actual != expected:
            raise ValueError("Restored native file differs from the captured artifact")
    verified_manifest(archive, digest, budget)
    complete(output)
    return {
        "restore_verified": True,
        "protocol_restore_verified": False,
        "configuration_required": True,
        "automatic_resume": False,
    }

"""Capture and independently reconstruct one explicitly quiesced Git workspace."""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from .workspace_archive_files import (
    DEFAULT_BUDGET,
    complete,
    json_bytes,
    new_directory,
    read_file,
    relative_file,
    verify_artifacts,
    write_file,
)
from .workspace_git import expanded_bytes, git, lines, snapshot, validate_repository


def object_bytes(directory):
    counts = dict(line.split(": ", 1) for line in lines(directory, "count-objects", "-v"))
    return 1024 * (int(counts["size"]) + int(counts["size-pack"]))


def identities(paths):
    return {str(path): [path.stat().st_dev, path.stat().st_ino] for path in paths}


def capture_artifacts(workspace, output, before, staged, unstaged, budget):
    git(
        workspace,
        "bundle",
        "create",
        str(output / "repository.bundle"),
        "--all",
        "HEAD",
        budget=budget,
    )
    git(workspace, "bundle", "verify", str(output / "repository.bundle"), budget=budget)
    write_file(output / "staged.patch", staged)
    write_file(output / "unstaged.patch", unstaged)
    extras = {}
    if before["selected"]:
        (output / "extras").mkdir(mode=0o700)
    for index, name in enumerate(before["selected"]):
        content, record = read_file(workspace, name, budget)
        if record != before["files"][name]:
            raise ValueError("Selected workspace file changed during capture")
        artifact = f"extras/{index:08d}.bin"
        write_file(output / artifact, content)
        extras[name] = artifact
    return extras


def capture_workspace(
    workspace, output, *, include=(), exclude=(), writers_stopped=False, budget=DEFAULT_BUDGET
):
    if not writers_stopped:
        raise ValueError("Workspace capture requires explicit confirmation that writers stopped")
    common, gitdir = validate_repository(workspace)
    if output.is_relative_to(workspace) or output.is_relative_to(common):
        raise ValueError("Workspace capture must be outside the source and Git metadata")
    started = datetime.now(UTC).isoformat()
    source_identity = identities((workspace, common, gitdir))
    before, staged, unstaged = snapshot(workspace, include, exclude, budget)
    source_bytes = sum(item["bytes"] for item in before["files"].values() if item)
    expanded = expanded_bytes(workspace)
    required = 4 * (
        source_bytes
        + sum(expanded.values())
        + object_bytes(workspace)
        + len(staged)
        + len(unstaged)
        + 4096
    )
    new_directory(output, budget, required)
    extras = capture_artifacts(workspace, output, before, staged, unstaged, budget)
    after, after_staged, after_unstaged = snapshot(workspace, include, exclude, budget)
    if (
        before != after
        or staged != after_staged
        or unstaged != after_unstaged
        or identities((workspace, common, gitdir)) != source_identity
    ):
        raise ValueError("Workspace changed during capture; incomplete artifacts retained")
    artifact_names = ["repository.bundle", "staged.patch", "unstaged.patch", *extras.values()]
    artifacts = {name: read_file(output, name, budget)[1] for name in artifact_names}
    total = sum(item["bytes"] for item in artifacts.values())
    manifest = {
        "version": 1,
        "kind": "offline-git-workspace",
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "source": {"workspace": str(workspace), "git_dir": str(gitdir), "common_dir": str(common)},
        "source_identity": source_identity,
        "writers_stopped_asserted": True,
        "source_stable_observed": True,
        "ignored_policy": "Excluded unless explicitly included; not enumerated or claimed complete",
        "restore_verified": False,
        "machine_execution": False,
        "state": before,
        "extras": extras,
        "artifacts": artifacts,
        "budget_bytes": budget,
        "estimated_peak_bytes": required,
        "expanded_bytes": expanded,
        "artifact_bytes": total,
    }
    content = json_bytes(manifest)
    if total + len(content) > budget:
        raise ValueError("Workspace artifacts exceed the byte budget")
    write_file(output / "manifest.json", content)
    complete(output)
    return {
        "captured": True,
        "restore_verified": False,
        "manifest_sha256": hashlib.sha256(content).hexdigest(),
        "artifact_bytes": total,
        "estimated_peak_bytes": required,
        "selected_files": len(include),
        "excluded_files": len(exclude),
        "machine_execution": False,
    }


def restore_files(output, archive, manifest, budget):
    for name, artifact in manifest["extras"].items():
        relative_file(name)
        path = output / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.resolve() != path:
            raise ValueError("Restored workspace path traverses symbolic links")
        content, _ = read_file(archive, artifact, budget)
        write_file(path, content)
    for name, record in manifest["state"]["files"].items():
        relative_file(name)
        path = output / name
        if record is not None:
            read_file(output, name, budget)
            if not isinstance(record["mode"], int) or not 0 <= record["mode"] <= 0o777:
                raise ValueError("Invalid restored file mode")
            os.chmod(path, record["mode"], follow_symlinks=False)


def rehearse_workspace(archive, output, digest, *, budget=DEFAULT_BUDGET):
    manifest = verify_artifacts(archive, digest, budget)
    state = manifest["state"]
    protected = [archive, *(Path(value) for value in manifest["source"].values())]
    if any(output.is_relative_to(path) for path in protected):
        raise ValueError("Restore into an independent private directory")
    for name in state["files"]:
        relative_file(name)
    required = 4 * (
        manifest["artifact_bytes"]
        + sum(manifest["expanded_bytes"].values())
        + sum(record["bytes"] for record in state["files"].values() if record)
        + 4096
    )
    new_directory(output, budget, required)
    git(output, "init", "--template=", budget=budget)
    (output / "INCOMPLETE").rename(output / ".git" / "INCOMPLETE")
    bundle = str(archive / "repository.bundle")
    git(output, "bundle", "verify", bundle, budget=budget)
    git(
        output,
        "-c",
        "protocol.file.allow=always",
        "fetch",
        "--no-write-fetch-head",
        bundle,
        "+refs/*:refs/*",
        budget=budget,
    )
    # Fetch a detached HEAD too: it need not be reachable from any named ref.
    git(
        output,
        "-c",
        "protocol.file.allow=always",
        "fetch",
        "--no-write-fetch-head",
        bundle,
        "HEAD",
        budget=budget,
    )
    git(output, "checkout", "--detach", state["head"], budget=budget)
    if state["head_ref"] != "HEAD":
        if not state["head_ref"].startswith("refs/heads/"):
            raise ValueError("Unsupported original HEAD reference")
        git(output, "symbolic-ref", "HEAD", state["head_ref"], budget=budget)
    for patch, arguments in [("staged.patch", ("--index",)), ("unstaged.patch", ())]:
        if manifest["artifacts"][patch]["bytes"]:
            git(output, "apply", "--binary", *arguments, str(archive / patch), budget=budget)
    restore_files(output, archive, manifest, budget)
    actual, staged, unstaged = snapshot(output, state["selected"], (), budget)
    expected = {
        **state,
        "excluded": [],
        "untracked": sorted(name for name in state["untracked"] if name in state["selected"]),
    }
    if actual != expected:
        raise ValueError("Restored workspace differs from captured state")
    for name, content in [("staged.patch", staged), ("unstaged.patch", unstaged)]:
        if content != read_file(archive, name, budget)[0]:
            raise ValueError("Restored Git changes differ from captured patches")
    # Re-read all artifacts after use; no source metadata/config is copied or executed.
    verify_artifacts(archive, digest, budget)
    complete(output / ".git")
    return {
        "restore_verified": True,
        "refs": len(state["refs"]),
        "selected_files": len(state["selected"]),
        "excluded_files": len(state["excluded"]),
        "machine_execution": False,
        "source_modified": False,
        "automatic_resume": False,
    }

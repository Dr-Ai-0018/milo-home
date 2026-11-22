import json
import os
import subprocess

import pytest

from milo_home.workspace_archive_files import DEFAULT_BUDGET
from milo_home.workspace_backup import capture_workspace, rehearse_workspace
from milo_home.workspace_git import git

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="Private archive management requires root"
)


def commit(directory, message):
    git(
        directory,
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.invalid",
        "commit",
        "-m",
        message,
    )


@pytest.fixture
def workspace_case(private_runtime_directory):
    root = private_runtime_directory("milo-workspace-backup-")
    source = root / "source"
    source.mkdir(mode=0o700)
    git(source, "init", "--template=", "--initial-branch=main")
    (source / "app.txt").write_text("original\n")
    (source / "binary.dat").write_bytes(b"\0original")
    (source / "deleted.txt").write_text("remove later\n")
    (source / ".gitignore").write_text("ignored.dat\n")
    git(source, "add", ".")
    commit(source, "Synthetic baseline")
    git(source, "tag", "synthetic-v1")
    (source / "app.txt").write_text("original\nstaged\n")
    (source / "binary.dat").write_bytes(b"\0staged")
    git(source, "add", "app.txt", "binary.dat")
    git(source, "rm", "deleted.txt")
    (source / "app.txt").write_text("original\nstaged\nunstaged\n")
    (source / "binary.dat").write_bytes(b"\0unstaged")
    (source / "app.txt").chmod(0o755)
    (source / "notes").mkdir()
    (source / "notes/中文 note.txt").write_text("Synthetic selected work\n")
    (source / "ignored.dat").write_text("Synthetic selected ignored work\n")
    (source / "credential.env").write_text("SYNTHETIC_SECRET_DO_NOT_ARCHIVE\n")
    return root, source


def capture(case, **options):
    root, source = case
    return capture_workspace(
        source,
        root / "archive",
        writers_stopped=True,
        include=["notes/中文 note.txt", "ignored.dat"],
        exclude=["credential.env"],
        **options,
    )


def test_actual_cli_round_trip_preserves_refs_both_diffs_and_selected_files(workspace_case):
    root, source = workspace_case
    index_before = (source / ".git/index").read_bytes()
    process = subprocess.run(
        [
            "milo-home",
            "backup-workspace",
            "capture",
            "--workspace",
            str(source),
            "--out",
            str(root / "archive"),
            "--writers-stopped",
            "--include-file",
            "notes/中文 note.txt",
            "--include-file",
            "ignored.dat",
            "--exclude-file",
            "credential.env",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "MILO_DATABASE": "/this-database-must-not-be-opened"},
    )
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    restored = root / "restored"
    process = subprocess.run(
        [
            "milo-home",
            "backup-workspace",
            "rehearse",
            "--archive",
            str(root / "archive"),
            "--out",
            str(restored),
            "--manifest-sha256",
            report["manifest_sha256"],
        ],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "MILO_DATABASE": "/this-database-must-not-be-opened"},
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["restore_verified"] and not result["automatic_resume"]
    assert result["refs"] == 2
    assert report["selected_files"] == 2 and report["excluded_files"] == 1
    assert report["estimated_peak_bytes"] < DEFAULT_BUDGET
    assert (source / ".git/index").read_bytes() == index_before
    assert (restored / "app.txt").read_text() == "original\nstaged\nunstaged\n"
    assert (restored / "app.txt").stat().st_mode & 0o777 == 0o755
    assert (restored / "binary.dat").read_bytes() == b"\0unstaged"
    assert not (restored / "deleted.txt").exists()
    assert (restored / "ignored.dat").read_bytes() == (source / "ignored.dat").read_bytes()
    assert not (restored / "credential.env").exists()
    for path in (root / "archive").rglob("*"):
        assert path.stat().st_mode & 0o077 == 0
        if path.is_file():
            assert b"SYNTHETIC_SECRET_DO_NOT_ARCHIVE" not in path.read_bytes()
    assert "中文" not in process.stdout and str(source) not in process.stdout


def test_linked_worktree_recovers_without_original_common_directory(workspace_case):
    root, source = workspace_case
    linked = root / "linked"
    git(source, "worktree", "add", "-b", "linked", str(linked))
    (linked / "linked.txt").write_text("Synthetic linked untracked\n")
    report = capture_workspace(
        linked, root / "archive", writers_stopped=True, include=["linked.txt"]
    )
    source.rename(root / "original-offline")
    linked.rename(root / "linked-offline")
    restored = root / "restored"
    result = rehearse_workspace(root / "archive", restored, report["manifest_sha256"])
    assert result["restore_verified"]
    assert (restored / ".git").is_dir()
    assert git(restored, "symbolic-ref", "HEAD").strip() == b"refs/heads/linked"
    assert (restored / "linked.txt").read_text() == "Synthetic linked untracked\n"


def test_detached_head_and_staged_add_then_deleted_file(workspace_case):
    root, source = workspace_case
    git(source, "checkout", "--detach")
    (source / "added.txt").write_text("Synthetic staged-only file\n")
    git(source, "add", "added.txt")
    (source / "added.txt").unlink()
    report = capture(workspace_case)
    restored = root / "restored"
    assert rehearse_workspace(root / "archive", restored, report["manifest_sha256"])[
        "restore_verified"
    ]
    assert git(restored, "rev-parse", "--symbolic-full-name", "HEAD").strip() == b"HEAD"
    assert b"added.txt" in git(restored, "diff", "--cached", "--name-only")
    assert not (restored / "added.txt").exists()


@pytest.mark.parametrize(
    "name", ["manifest.json", "staged.patch", "repository.bundle", "extras/00000000.bin"]
)
def test_tampered_artifact_refused_before_creating_restore(workspace_case, name):
    root, _ = workspace_case
    report = capture(workspace_case)
    with (root / "archive" / name).open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="differs"):
        rehearse_workspace(root / "archive", root / "restored", report["manifest_sha256"])
    assert not (root / "restored").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "notes/../bad",
        ".git/config",
        "credential.env",
        "auth.json",
        ".env.local",
    ],
)
def test_unsafe_or_credential_selection_refused(workspace_case, name):
    root, source = workspace_case
    if name in {"auth.json", ".env.local"}:
        (source / name).write_text("synthetic credential")
    with pytest.raises(ValueError):
        capture_workspace(
            source,
            root / "archive",
            writers_stopped=True,
            include=[name],
            exclude=["credential.env", "notes/中文 note.txt"],
        )
    assert not (root / "archive").exists()


def test_unclassified_new_files_and_missing_stop_confirmation_refused(workspace_case):
    root, source = workspace_case
    with pytest.raises(ValueError, match="confirmation"):
        capture_workspace(source, root / "archive")
    with pytest.raises(ValueError, match="include or exclude"):
        capture_workspace(source, root / "archive", writers_stopped=True)
    assert not (root / "archive").exists()


def test_source_change_retains_incomplete_evidence(workspace_case, monkeypatch):
    import milo_home.workspace_backup as module

    original = module.snapshot
    calls = 0

    def changing(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            (workspace_case[1] / "app.txt").write_text("Synthetic changed during capture\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "snapshot", changing)
    with pytest.raises(ValueError, match="changed during capture"):
        capture(workspace_case)
    archive = workspace_case[0] / "archive"
    assert (archive / "INCOMPLETE").exists()
    assert not (archive / "manifest.json").exists()


def test_budget_and_disk_refused_without_creating_output(workspace_case, monkeypatch):
    import milo_home.workspace_archive_files as files

    root, _ = workspace_case
    with pytest.raises(ValueError, match="budget"):
        capture(workspace_case, budget=1)
    monkeypatch.setattr(files.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 0})())
    with pytest.raises(ValueError, match="disk space"):
        capture(workspace_case)
    assert not (root / "archive").exists()


def test_no_overwrite_and_linked_artifact_refused(workspace_case):
    root, _ = workspace_case
    report = capture(workspace_case)
    manifest_before = (root / "archive/manifest.json").read_bytes()
    with pytest.raises(ValueError, match="new canonical"):
        capture(workspace_case)
    assert (root / "archive/manifest.json").read_bytes() == manifest_before
    target = root / "archive/unstaged.patch"
    target.rename(root / "saved.patch")
    target.symlink_to(root / "saved.patch")
    with pytest.raises(ValueError, match="symbolic links"):
        rehearse_workspace(root / "archive", root / "restored", report["manifest_sha256"])
    assert not (root / "restored").exists()


def test_source_symlink_and_transform_attributes_refused(workspace_case):
    root, source = workspace_case
    target = source / "notes/中文 note.txt"
    target.unlink()
    target.symlink_to(source / "app.txt")
    with pytest.raises(ValueError, match="symbolic links"):
        capture(workspace_case)
    target.unlink()
    target.write_text("Synthetic note\n")
    (source / ".gitattributes").write_text("*.txt filter=custom\n")
    with pytest.raises(ValueError):
        capture(workspace_case)
    assert not (root / "archive").exists()


def test_git_routing_environment_does_not_redirect_source(workspace_case, monkeypatch):
    root, source = workspace_case
    monkeypatch.setenv("GIT_DIR", str(root / "nonexistent"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(root / "must-not-create-index"))
    report = capture(workspace_case)
    assert rehearse_workspace(root / "archive", root / "restored", report["manifest_sha256"])[
        "restore_verified"
    ]
    assert not (root / "must-not-create-index").exists()
    assert (source / ".git/index").exists()


@pytest.mark.parametrize(
    "unsupported",
    ["shallow", "alternates", "partial", "symlink", "submodule", "attributes", "sparse"],
)
def test_unsupported_git_states_are_explicitly_refused(workspace_case, unsupported):
    root, source = workspace_case
    if unsupported == "shallow":
        (source / ".git/shallow").write_text("synthetic marker")
    elif unsupported == "alternates":
        (source / ".git/objects/info/alternates").write_text("/synthetic-missing-objects\n")
    elif unsupported == "partial":
        git(source, "config", "remote.origin.promisor", "true")
    elif unsupported == "attributes":
        git(source, "config", "core.autocrlf", "true")
    elif unsupported == "sparse":
        git(source, "update-index", "--skip-worktree", "app.txt")
    elif unsupported == "symlink":
        (source / "linked-file").symlink_to("app.txt")
        git(source, "add", "linked-file")
    else:
        head = git(source, "rev-parse", "HEAD").decode().strip()
        git(source, "update-index", "--add", "--cacheinfo", f"160000,{head},submodule")
    with pytest.raises(ValueError):
        capture(workspace_case)
    assert not (root / "archive").exists()


def test_external_diff_fsmonitor_and_checkout_hooks_never_run(workspace_case):
    root, source = workspace_case
    helper = root / "unexpected-helper"
    helper.write_text(f"#!/bin/sh\ntouch '{root / 'HELPER-RAN'}'\n")
    helper.chmod(0o700)
    git(source, "config", "diff.external", str(helper))
    git(source, "config", "core.fsmonitor", str(helper))
    hooks = source / ".git/hooks"
    hooks.mkdir()
    (hooks / "post-checkout").write_bytes(helper.read_bytes())
    (hooks / "post-checkout").chmod(0o700)
    report = capture(workspace_case)
    assert rehearse_workspace(root / "archive", root / "restored", report["manifest_sha256"])[
        "restore_verified"
    ]
    assert not (root / "HELPER-RAN").exists()
    assert not (root / "restored/.git/hooks").exists()


def test_peak_budget_counts_expanded_head_and_index_blobs(workspace_case):
    root, source = workspace_case
    # A highly compressible staged-only blob is absent from the current worktree.
    (source / "compressed.txt").write_bytes(b"x" * 2 * 1024 * 1024)
    git(source, "add", "compressed.txt")
    (source / "compressed.txt").unlink()
    report = capture(workspace_case)
    manifest = json.loads((root / "archive/manifest.json").read_text())
    assert manifest["expanded_bytes"]["index"] >= 2 * 1024 * 1024
    assert report["estimated_peak_bytes"] >= 8 * 1024 * 1024
    assert rehearse_workspace(root / "archive", root / "restored", report["manifest_sha256"])[
        "restore_verified"
    ]


def test_restore_cannot_write_inside_original_source_or_metadata(workspace_case):
    root, source = workspace_case
    report = capture(workspace_case)
    for destination in [source / "restored", source / ".git/restored", root / "archive/restored"]:
        with pytest.raises(ValueError, match="independent"):
            rehearse_workspace(root / "archive", destination, report["manifest_sha256"])
        assert not destination.exists()


def test_local_diff_display_preferences_do_not_change_recovery(workspace_case):
    root, source = workspace_case
    for key, value in [
        ("color.ui", "always"),
        ("diff.noprefix", "true"),
        ("diff.context", "8"),
        ("diff.algorithm", "patience"),
    ]:
        git(source, "config", key, value)
    report = capture(workspace_case)
    assert rehearse_workspace(root / "archive", root / "restored", report["manifest_sha256"])[
        "restore_verified"
    ]

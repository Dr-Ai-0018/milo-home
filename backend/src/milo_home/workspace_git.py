"""Read Git state without hooks, inherited Git routing or external diff helpers."""

import os
import resource
import subprocess
from pathlib import Path

from .workspace_archive_files import credential_path, read_file, relative_file


def git(directory, *arguments, budget=256 * 1024 * 1024, input_data=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        LC_ALL="C",
    )

    def limit():
        resource.setrlimit(resource.RLIMIT_FSIZE, (budget, budget))
        os.umask(0o077)

    try:
        process = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-C",
                str(directory),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.filemode=true",
                "-c",
                "core.attributesFile=/dev/null",
                "-c",
                "core.quotePath=true",
                "-c",
                "diff.orderFile=/dev/null",
                *arguments,
            ],
            env=environment,
            capture_output=True,
            input=input_data,
            timeout=20,
            preexec_fn=limit,
        )
    except subprocess.SubprocessError:
        raise ValueError("Offline Git operation exceeded its finite limit") from None
    if process.returncode or len(process.stdout) > budget:
        raise ValueError("Offline Git operation failed; private source details suppressed")
    return process.stdout


def lines(directory, *arguments):
    return git(directory, *arguments).decode("utf-8").splitlines()


def validate_repository(directory):
    if not directory.is_absolute() or directory.resolve() != directory:
        raise ValueError("Workspace must be a canonical absolute directory")
    top = Path(lines(directory, "rev-parse", "--show-toplevel")[0])
    if top != directory:
        raise ValueError("Select the exact Git workspace root")
    common = Path(lines(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")[0])
    gitdir = Path(lines(directory, "rev-parse", "--absolute-git-dir")[0])
    for name in ("shallow", "objects/info/alternates", "info/grafts", "info/attributes"):
        if (common / name).exists():
            raise ValueError("Git workspace has unsupported external or transformed state")
    config = lines(directory, "config", "--local", "--list")
    if any(".promisor=" in item or item.startswith("extensions.partialclone=") for item in config):
        raise ValueError("Partial Git workspace is unsupported")
    if any(item in {"core.autocrlf=true", "core.autocrlf=input"} for item in config):
        raise ValueError("Line-ending transformations require separate backup support")
    if git(directory, "ls-files", "-u") or git(directory, "for-each-ref", "refs/replace/"):
        raise ValueError("Unmerged or replaced Git objects are unsupported")
    entries = git(directory, "ls-files", "--stage", "-z").split(b"\0")
    if any(item and item.split(b" ", 1)[0] not in {b"100644", b"100755"} for item in entries):
        raise ValueError("Git links, submodules or non-regular tracked files are unsupported")
    flags = git(directory, "ls-files", "-v", "-z").split(b"\0")
    if any(item and item[:1] != b"H" for item in flags):
        raise ValueError("Sparse or assume-unchanged Git files are unsupported")
    for path in (common, gitdir, common / "objects"):
        if path.resolve() != path:
            raise ValueError("Git metadata must not traverse symbolic links")
    if list((common / "objects/pack").glob("*.promisor")):
        raise ValueError("Partial Git workspace is unsupported")
    return common, gitdir


def names(directory, *arguments):
    return [
        item.decode("utf-8")
        for item in git(directory, "ls-files", "-z", *arguments).split(b"\0")
        if item
    ]


def expanded_bytes(directory):
    tree = git(directory, "ls-tree", "-r", "-l", "-z", "HEAD").split(b"\0")
    head = 0
    for entry in filter(None, tree):
        metadata = entry.split(b"\t", 1)[0].split()
        if metadata[0] not in {b"100644", b"100755"}:
            raise ValueError("Original HEAD links or submodules require separate backup support")
        head += int(metadata[3])
    entries = git(directory, "ls-files", "--stage", "-z").split(b"\0")
    objects = [entry.split(b" ")[1] for entry in entries if entry]
    sizes = (
        git(
            directory,
            "cat-file",
            "--batch-check=%(objectsize)",
            input_data=b"\n".join(objects) + b"\n",
        )
        if objects
        else b""
    )
    return {"head": head, "index": sum(int(size) for size in sizes.splitlines())}


def inventory(directory, tracked, selected, budget):
    files = {}
    total = 0
    for name in sorted(set(tracked) | set(selected)):
        relative_file(name)
        if credential_path(name):
            raise ValueError("Authentication paths cannot be included in a workspace archive")
        path = directory / name
        if path.resolve() != path:
            raise ValueError("Workspace files must not traverse symbolic links")
        ancestors = [path.parent, *path.parent.parents]
        for parent in ancestors:
            if parent.is_relative_to(directory) and (parent / ".gitattributes").exists():
                raise ValueError("Attribute transformations require separate backup support")
        if not path.exists() and not path.is_symlink() and name in tracked:
            files[name] = None
            continue
        content, record = read_file(directory, name, budget)
        total += len(content)
        if total > budget:
            raise ValueError("Workspace files exceed the byte budget")
        files[name] = record
    return files


def snapshot(directory, selected, excluded, budget):
    tracked = names(directory)
    others = names(directory, "--others", "--exclude-standard")
    if (
        len(set(selected)) != len(selected)
        or len(set(excluded)) != len(excluded)
        or set(selected) & set(excluded)
    ):
        raise ValueError("Workspace file selection is ambiguous")
    if set(others) - set(selected) - set(excluded) or set(excluded) - set(others):
        raise ValueError("Every non-ignored untracked file needs an include or exclude decision")
    if set(selected) & set(tracked):
        raise ValueError("Select only untracked or ignored extra files")
    files = inventory(directory, tracked, selected, budget)
    attributes = git(directory, "ls-files", "--", "**/.gitattributes", ".gitattributes")
    if attributes:
        raise ValueError("Attribute transformations require separate backup support")
    head = lines(directory, "rev-parse", "HEAD")[0]
    ref = lines(directory, "rev-parse", "--symbolic-full-name", "HEAD")[0]
    refs = lines(directory, "for-each-ref", "--format=%(refname) %(objectname)")
    diff_args = (
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--no-color",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "--unified=3",
        "--inter-hunk-context=0",
        "--diff-algorithm=myers",
        "--no-indent-heuristic",
    )
    staged = git(directory, "diff", *diff_args, "--cached", "HEAD", "--", budget=budget)
    unstaged = git(directory, "diff", *diff_args, "--", budget=budget)
    return (
        {
            "head": head,
            "head_ref": ref,
            "refs": refs,
            "index": git(directory, "ls-files", "--stage", "-z").decode("utf-8"),
            "files": files,
            "selected": sorted(selected),
            "excluded": sorted(excluded),
            "untracked": sorted(others),
        },
        staged,
        unstaged,
    )

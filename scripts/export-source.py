"""Copy the reviewed source list into a new directory, without Git history."""

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "public-source-files.json"
DIRECTORIES = {
    "backend",
    "contracts",
    "deploy",
    "examples",
    "frontend",
    "scripts",
}
ROOT_FILES = {".env.example", ".gitignore", "LICENSE", "README.md", MANIFEST}


def reviewed_files():
    document = json.loads((ROOT / MANIFEST).read_text())
    names = document["files"]
    if document["version"] != 1 or not names or names != sorted(set(names)):
        raise ValueError("Source manifest must be a sorted, unique version-one list")
    result = {}
    for name in names:
        relative = PurePosixPath(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or str(relative) != name
            or (relative.parts[0] not in DIRECTORIES and name not in ROOT_FILES)
            or any(
                part in {".git", "node_modules", ".venv", "__pycache__", "dist"}
                for part in relative.parts
            )
            or (relative.name.startswith(".env") and relative.name != ".env.example")
        ):
            raise ValueError("Source manifest includes a forbidden path")
        source = ROOT / name
        if source.resolve() != source or not stat.S_ISREG(source.lstat().st_mode):
            raise ValueError("Reviewed source must be an existing non-linked file")
        result[name] = (
            source.read_bytes(),
            0o755 if source.stat().st_mode & 0o111 else 0o644,
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        help="New directory outside this source tree; omitted checks only",
    )
    args = parser.parse_args()
    try:
        files = reviewed_files()
        if args.out is not None:
            if not args.out.is_absolute() or args.out.resolve().is_relative_to(ROOT):
                raise ValueError(
                    "Source export requires an external absolute directory"
                )
            args.out.mkdir(mode=0o700)
            for name, (content, mode) in files.items():
                destination = args.out / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(
                    destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
        digest = hashlib.sha256()
        for name, (content, _) in files.items():
            digest.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
        print(
            json.dumps(
                {
                    "files": len(files),
                    "source_sha256": digest.hexdigest(),
                    "git_history": False,
                }
            )
        )
    except (ValueError, KeyError, TypeError, OSError):
        parser.exit(
            1,
            "Source export failed; check the reviewed manifest and new output directory.\n",
        )


if __name__ == "__main__":
    main()

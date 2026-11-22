"""Bounded, private copies of original per-run native reply recovery routes."""

import hashlib
import json
import stat
from uuid import UUID

from .database_identity import private_run_directory
from .native_reply_route import REQUIRED, ROUTE, has_native_route
from .workspace_archive_files import (
    complete,
    json_bytes,
    new_directory,
    read_file,
    write_file,
)


def route_inventory(directory, runs, home, fingerprint, budget):
    private_run_directory(directory)
    files, routes, total = {}, {}, 0
    known = {run["id"] for run in runs}
    for count, path in enumerate(directory.iterdir(), 1):
        if count > 20000:
            raise ValueError("Reply source exceeds its directory bound")
        try:
            identity = str(UUID(path.name))
        except ValueError:
            continue
        if identity == path.name and has_native_route(path) and identity not in known:
            raise ValueError("Native reply route has no original business run")
    for run in runs:
        identity = str(UUID(run["id"]))
        base = directory / identity
        present = [
            (base / name).exists() or (base / name).is_symlink() for name in (REQUIRED, ROUTE)
        ]
        if not any(present):
            continue
        private_run_directory(base)
        if not all(present):
            raise ValueError("Native reply route and required marker must both exist")
        content = {}
        for name in (REQUIRED, ROUTE):
            key = identity + "/" + name
            content[name], metadata = read_file(directory, key, min(budget - total, 128 * 1024))
            if (base / name).stat().st_uid != 0 or metadata["mode"] != 0o600:
                raise ValueError("Native reply route must be root-owned and private")
            files[key] = metadata
            total += metadata["bytes"]
        route = json.loads(content[ROUTE])
        expected = {
            "home": str(home),
            "fingerprint": fingerprint,
            "owner": run["owner_id"],
            "run": identity,
        }
        if content[REQUIRED] != b"1\n" or route != expected:
            raise ValueError("Native reply route differs from its original run or main")
        routes[identity] = route
    return {"files": files, "routes": routes, "bytes": total}


def capture_routes(directory, output, runs, home, fingerprint, budget):
    before = route_inventory(directory, runs, home, fingerprint, budget)
    identity = [directory.stat().st_dev, directory.stat().st_ino]
    new_directory(output, budget, before["bytes"] * 4 + 1024 * 1024)
    for name in before["files"]:
        destination = output / "routes" / name
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        (output / "routes").chmod(0o700)
        write_file(destination, read_file(directory, name, budget)[0])
    # Empty scope remains an explicit component of the paired generation.
    (output / "routes").mkdir(mode=0o700, exist_ok=True)
    if route_inventory(directory, runs, home, fingerprint, budget) != before or identity != [
        directory.stat().st_dev,
        directory.stat().st_ino,
    ]:
        raise ValueError("Native reply routes changed during capture")
    manifest = {
        "version": 1,
        "kind": "offline-native-reply-routes",
        "source": str(directory),
        "source_identity": identity,
        "inventory": before,
        "automatic_resume": False,
    }
    content = json_bytes(manifest)
    if len(content) + before["bytes"] > budget:
        raise ValueError("Native reply route archive exceeds its byte budget")
    write_file(output / "manifest.json", content)
    complete(output)
    return {"manifest_sha256": hashlib.sha256(content).hexdigest()}


def verified_routes(archive, digest, runs, home, fingerprint, budget):
    private_run_directory(archive)
    if (archive / "INCOMPLETE").exists():
        raise ValueError("Native reply route archive is incomplete")
    content, _ = read_file(archive, "manifest.json", budget)
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Native reply route manifest differs from its trusted digest")
    manifest = json.loads(content)
    if (
        manifest.get("version") != 1
        or manifest.get("kind") != "offline-native-reply-routes"
        or manifest.get("automatic_resume") is not False
    ):
        raise ValueError("Unsupported native reply route archive")
    actual = route_inventory(archive / "routes", runs, home, fingerprint, budget)
    names = set()
    for count, path in enumerate((archive / "routes").rglob("*"), 1):
        if count > 40000 or path.resolve() != path:
            raise ValueError("Native reply route archive contains links or excess entries")
        metadata = path.lstat()
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != (
            0o700 if path.is_dir() else 0o600
        ):
            raise ValueError("Native reply route archive must remain private")
        if not path.is_dir():
            names.add(path.relative_to(archive / "routes").as_posix())
    if actual != manifest["inventory"] or names != set(actual["files"]):
        raise ValueError("Native reply route artifacts differ from their manifest")
    return manifest


def rehearse_routes(archive, output, digest, runs, home, fingerprint, budget):
    manifest = verified_routes(archive, digest, runs, home, fingerprint, budget)
    new_directory(output, budget, manifest["inventory"]["bytes"] * 2)
    for name in manifest["inventory"]["files"]:
        destination = output / name
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_file(destination, read_file(archive / "routes", name, budget)[0])
    if route_inventory(output, runs, home, fingerprint, budget) != manifest["inventory"]:
        raise ValueError("Restored native reply routes differ")
    complete(output)
    return {"routes_restored": len(manifest["inventory"]["routes"]), "automatic_resume": False}

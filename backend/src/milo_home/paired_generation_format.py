"""Generation-level file inventory and explicit optional component scope."""

from .workspace_archive_files import read_file


def artifact_inventory(directory, budget):
    artifacts = {}
    total = 0
    for count, path in enumerate(directory.rglob("*"), 1):
        if count > 40000:
            raise ValueError("Paired archive exceeds its entry count bound")
        name = path.relative_to(directory).as_posix()
        if name in {"manifest.json", "INCOMPLETE"}:
            continue
        if path.resolve() != path:
            raise ValueError("Paired archive must not contain links")
        if path.is_dir():
            if path.stat().st_mode & 0o777 != 0o700:
                raise ValueError("Paired archive directories must be private")
            continue
        _, record = read_file(directory, name, budget - total)
        total += record["bytes"]
        if record["mode"] != 0o600 or len(artifacts) >= 40000:
            raise ValueError("Paired archive file mode or count exceeds its bounds")
        artifacts[name] = record
    return artifacts


def reply_scope(manifest):
    """v1/v2 retain old meaning; v4 derives replies from the whole explicit scope."""
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int:
        raise ValueError("Unsupported paired generation")
    version = manifest["version"]
    components = manifest.get("components")
    if version not in {1, 2, 3, 4} or not isinstance(components, dict):
        raise ValueError("Unsupported paired generation")
    replies = version == 3 or (version == 4 and "replies" in components)
    expected = {"runtime", "native", "workspace"} | ({"replies"} if replies else set())
    if set(components) != expected:
        raise ValueError("Paired components differ from their declared version")
    baseline = manifest.get("baseline", {})
    if not isinstance(baseline, dict) or not isinstance(baseline.get("business"), dict):
        raise ValueError("Invalid paired business scope")
    business = baseline.get("business", {})
    present = [
        "reply_source" in manifest,
        "reply_routes" in baseline,
        "reply_source_identity" in baseline,
        "native_replies" in business,
    ]
    if any(present) != replies or (replies and not all(present)):
        raise ValueError("Paired reply component and its sources or mapping disagree")
    return replies

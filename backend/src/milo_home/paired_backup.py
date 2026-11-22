"""One trusted generation of business, runtime, native state and Git artifacts."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .backup_process_evidence import observe_recorded, verify_observation_sources
from .backup_rehearsal import rehearse_backup
from .database import CURRENT_SCHEMA_VERSION, Database
from .database_identity import private_run_directory
from .database_inspection import inspect_database
from .execution_lock import execution_lock
from .native_session_backup import (
    independent_output,
    rehearse_native,
    verified_manifest,
)
from .paired_attachment_backup import (
    attachment_index,
    capture_attachments,
    restore_attachments,
    verify_attachment_archive,
    verify_attachment_source,
)
from .paired_backup_components import capture_components
from .paired_backup_state import (
    business_snapshot,
    business_state,
    check_association,
    journal_thread,
    rehearse_historical_business,
    source_state,
)
from .paired_generation_format import artifact_inventory, reply_scope
from .public_asset_backup import (
    capture_public_assets,
    public_asset_index,
    restore_public_assets,
    verify_public_archive,
    verify_public_source,
)
from .reply_route_backup import rehearse_routes, verified_routes
from .runtime_evidence_backup import rehearse_runtime, verified_runtime
from .workspace_archive_files import (
    DEFAULT_BUDGET,
    complete,
    json_bytes,
    new_directory,
    read_file,
    verify_artifacts,
    write_file,
)
from .workspace_backup import rehearse_workspace


def _publish_generation(output, manifest, lease, budget):
    content = json_bytes(manifest)
    total = len(content) + sum(r["bytes"] for r in manifest["artifacts"].values())
    if total > budget:
        raise ValueError("Paired generation exceeds its total byte budget")
    write_file(output / "manifest.json", content)
    lease.verify()
    complete(output)
    return {
        "captured": True,
        "generation": manifest["generation"],
        "manifest_sha256": hashlib.sha256(content).hexdigest(),
        "artifact_bytes": total,
        "quiescence_verified": False,
        "automatic_resume": False,
    }


def _generation_manifest(
    home, workspace, output, started, before, components, observations, budget
):
    return {
        "version": 4,
        "kind": "offline-paired-generation",
        "generation": uuid4().hex,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "home": str(home),
        "workspace": str(workspace),
        "baseline": before,
        "components": components,
        "process_observations": {"before": observations[0], "after": observations[1]},
        "artifacts": artifact_inventory(output, budget),
        "writers_stopped_confirmed": True,
        "source_stable_observed": True,
        "quiescence_verified": False,
        "automatic_resume": False,
    }


def _check_asset_roots(stores, home, workspace, output):
    for store in stores:
        if store is not None:
            store.check_workspace(workspace)
            independent_output(store.root, output)
            independent_output(store.root, home)
    if all(store is not None for store in stores):
        independent_output(stores[0].root, stores[1].root)


def capture_generation(
    database,
    home,
    workspace,
    output,
    *,
    writers_stopped=False,
    include=(),
    exclude=(),
    native_exclude=(),
    budget=DEFAULT_BUDGET,
    attachment_store=None,
    public_asset_store=None,
    reply_directory=None,
):
    if not writers_stopped:
        raise ValueError("Paired capture requires explicit stopped-writer confirmation")
    for source in (database.path.parent, home, workspace):
        independent_output(source, output)
    if reply_directory is not None:
        independent_output(reply_directory, output)
    _check_asset_roots((attachment_store, public_asset_store), home, workspace, output)
    started = datetime.now(UTC).isoformat()
    with execution_lock(database) as lease:
        options = dict(
            include=include,
            exclude=exclude,
            native_exclude=native_exclude,
            budget=budget,
            reply_directory=reply_directory,
        )
        before = source_state(database, home, workspace, lease, **options)
        attachments = attachment_index(database, budget)
        if attachments and attachment_store is None:
            raise ValueError("Paired capture requires the indexed attachment store")
        assets = public_asset_index(database, budget)
        if assets and public_asset_store is None:
            raise ValueError("Paired capture requires the indexed public asset store")
        asset_bytes = sum(item["size_bytes"] for item in [*attachments, *assets])
        before["estimated_peak_bytes"] += 4 * asset_bytes
        process_before = observe_recorded(home, budget=budget)
        new_directory(output, budget, before["estimated_peak_bytes"])
        # Root-private output is never accessed while ServiceDatabase demotes the UID.
        business_snapshot(database, output / "business.sqlite3", budget)
        if not inspect_database(output / "business.sqlite3")["database_ok"]:
            raise ValueError("Captured business snapshot failed inspection")
        snapshot = Database(output / "business.sqlite3")
        if (
            business_state(snapshot, budget, native_replies=reply_directory is not None)
            != before["business"]
        ):
            raise ValueError("Captured business snapshot differs from the generation baseline")
        if attachment_index(snapshot, budget) != attachments:
            raise ValueError("Captured business snapshot has a different attachment index")
        if public_asset_index(snapshot, budget) != assets:
            raise ValueError("Captured business snapshot has a different public asset index")
        if attachments:
            capture_attachments(output, attachment_store, attachments)
        capture_public_assets(output, public_asset_store, assets)
        components = capture_components(home, workspace, output, before, options)
        process_after = observe_recorded(home, budget=budget)
        if process_before["families"] != process_after["families"]:
            raise ValueError("Recorded process families changed during capture")
        after = source_state(database, home, workspace, lease, **options)
        after["estimated_peak_bytes"] += 4 * asset_bytes
        if (
            after != before
            or attachment_index(database, budget) != attachments
            or public_asset_index(database, budget) != assets
        ):
            raise ValueError("A paired source changed across the generation capture")
        if attachments:
            verify_attachment_source(attachment_store, attachments)
        verify_public_source(public_asset_store, assets)
        manifest = _generation_manifest(
            home,
            workspace,
            output,
            started,
            before,
            components,
            (process_before, process_after),
            budget,
        )
        manifest["public_assets"] = {
            "source": str(public_asset_store.root) if public_asset_store else None,
            "index": assets,
        }
        if reply_directory is not None:
            manifest["reply_source"] = str(reply_directory)
        if attachments:
            manifest["attachment_source"] = str(attachment_store.root)
            verify_attachment_archive(manifest, attachments)
        verify_public_archive(manifest, assets)
        return _publish_generation(output, manifest, lease, budget)


def verified_generation(archive, digest, budget):
    private_run_directory(archive)
    if (archive / "INCOMPLETE").exists():
        raise ValueError("Paired generation is incomplete")
    content, _ = read_file(archive, "manifest.json", budget)
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Paired manifest differs from the trusted digest")
    manifest = json.loads(content)
    replies_included = reply_scope(manifest)
    if manifest.get("kind") != "offline-paired-generation" or not isinstance(
        manifest.get("artifacts"), dict
    ):
        raise ValueError("Unsupported paired generation")
    actual = artifact_inventory(archive, budget)
    if (
        actual != manifest["artifacts"]
        or len(content) + sum(r["bytes"] for r in actual.values()) > budget
    ):
        raise ValueError("Paired artifacts differ from the total manifest or budget")
    digests = manifest["components"]
    schema = manifest["baseline"]["business"]["schema"]
    attachments = attachment_index(Database(archive / "business.sqlite3"), budget)
    assets = public_asset_index(Database(archive / "business.sqlite3"), budget)
    verify_public_archive(manifest, assets)
    if bool(attachments) != (manifest["version"] == 2 or "attachment_source" in manifest):
        raise ValueError("Paired generation is missing its attachment scope")
    if attachments:
        source = Path(manifest["attachment_source"])
        if not source.is_absolute() or ".." in source.parts:
            raise ValueError("Invalid archived attachment source")
    verify_attachment_archive(manifest, attachments)
    runtime = verified_runtime(archive / "runtime", digests["runtime"], budget)
    native = verified_manifest(archive / "native", digests["native"], budget)
    workspace = verify_artifacts(archive / "workspace", digests["workspace"], budget)
    workspace_digest = hashlib.sha256(
        json_bytes(
            {
                "state": workspace["state"],
                "staged": workspace["artifacts"]["staged.patch"]["sha256"],
                "unstaged": workspace["artifacts"]["unstaged.patch"]["sha256"],
            }
        )
    ).hexdigest()
    if (
        runtime["source"] != manifest["home"]
        or native["source"] != manifest["home"]
        or native["thread_id"] != manifest["baseline"]["thread_id"]
        or workspace["source"]["workspace"] != manifest["workspace"]
        or workspace_digest != manifest["baseline"]["workspace_sha256"]
        or runtime["inventory"] != manifest["baseline"]["runtime"]
        or native["inventory"] != manifest["baseline"]["native"]
        or journal_thread(archive / "runtime/evidence") != manifest["baseline"]["thread_id"]
        or business_state(
            Database(archive / "business.sqlite3"),
            budget,
            archive_schema=schema,
            native_replies=replies_included,
        )
        != manifest["baseline"]["business"]
    ):
        raise ValueError("Paired component sources differ from the generation")
    reply_routes = None
    if replies_included:
        replies = verified_routes(
            archive / "replies",
            digests["replies"],
            manifest["baseline"]["business"]["native_replies"]["runs"],
            Path(manifest["home"]),
            manifest["baseline"]["binding"]["fingerprint"],
            budget,
        )
        if (
            replies["source"] != manifest["reply_source"]
            or replies["inventory"] != manifest["baseline"]["reply_routes"]
            or replies["source_identity"] != manifest["baseline"]["reply_source_identity"]
        ):
            raise ValueError("Paired reply routes differ from the generation baseline")
        reply_routes = replies["inventory"]
    check_association(
        manifest["baseline"]["business"],
        archive / "runtime/evidence",
        Path(manifest["workspace"]),
        manifest["baseline"]["binding"],
        source_home=Path(manifest["home"]),
        reply_routes=reply_routes,
        budget=budget,
    )
    verify_observation_sources(archive, manifest, budget=budget)
    return manifest


def rehearse_generation(archive, output, digest, *, budget=DEFAULT_BUDGET):
    manifest = verified_generation(archive, digest, budget)
    replies_included = reply_scope(manifest)
    for source in (
        archive,
        Path(manifest["home"]),
        Path(manifest["workspace"]),
        Path(manifest["baseline"]["binding"]["database"]).parent,
    ):
        independent_output(source, output)
    if "attachment_source" in manifest:
        independent_output(Path(manifest["attachment_source"]), output)
    public_source = manifest.get("public_assets", {}).get("source")
    if public_source is not None:
        independent_output(Path(public_source), output)
    if replies_included:
        independent_output(Path(manifest["reply_source"]), output)
    new_directory(output, budget, manifest["baseline"]["estimated_peak_bytes"])
    schema = manifest["baseline"]["business"]["schema"]
    if schema == CURRENT_SCHEMA_VERSION:
        business = rehearse_backup(archive / "business.sqlite3", output / "business.sqlite3")
    else:
        business = rehearse_historical_business(
            Database(archive / "business.sqlite3"), output / "business.sqlite3", budget, schema
        )
    digests = manifest["components"]
    runtime = rehearse_runtime(
        archive / "runtime", output / "runtime", digests["runtime"], budget=budget
    )
    native = rehearse_native(
        archive / "native", output / "native", digests["native"], budget=budget
    )
    workspace = rehearse_workspace(
        archive / "workspace", output / "workspace", digests["workspace"], budget=budget
    )
    copied = business_state(
        Database(output / "business.sqlite3"),
        budget,
        archive_schema=schema,
        native_replies=replies_included,
    )
    if copied != manifest["baseline"]["business"]:
        raise ValueError("Restored business content differs from the generation")
    replies = None
    if replies_included:
        replies = rehearse_routes(
            archive / "replies",
            output / "replies",
            digests["replies"],
            copied["native_replies"]["runs"],
            Path(manifest["home"]),
            manifest["baseline"]["binding"]["fingerprint"],
            budget,
        )
    check_association(
        copied,
        output / "runtime",
        Path(manifest["workspace"]),
        manifest["baseline"]["binding"],
        source_home=Path(manifest["home"]),
        reply_routes=manifest["baseline"].get("reply_routes"),
        budget=budget,
    )
    attachments = restore_attachments(
        archive, output, attachment_index(Database(output / "business.sqlite3"), budget), budget
    )
    assets = restore_public_assets(
        archive, output, public_asset_index(Database(output / "business.sqlite3"), budget), budget
    )
    verified_generation(archive, digest, budget)
    report = {
        "generation": manifest["generation"],
        "file_restore_verified": True,
        "business": business,
        "runtime": runtime,
        "native": native,
        "workspace": workspace,
        "attachments_restored": attachments,
        "public_assets_restored": assets,
        "quiescence_verified": False,
        "protocol_restore_verified": False,
        "execution_binding_verified": False,
        "remote_verified": False,
        "automatic_resume": False,
    }
    if replies is not None:
        report["native_replies"] = replies
    write_file(output / "recovery-report.json", json_bytes(report))
    complete(output)
    return report

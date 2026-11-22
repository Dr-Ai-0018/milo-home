"""Capture the private/native/Git/route components after the common baseline is fixed."""

from .native_session_backup import capture_native
from .paired_backup_state import native_exclusions
from .reply_route_backup import capture_routes
from .runtime_evidence_backup import capture_runtime
from .workspace_backup import capture_workspace


def capture_components(home, workspace, output, baseline, options):
    budget = options["budget"]
    runtime = capture_runtime(home, output / "runtime", writers_stopped=True, budget=budget)
    native = capture_native(
        home,
        output / "native",
        baseline["thread_id"],
        writers_stopped=True,
        exclude=native_exclusions(home, options["native_exclude"], budget=budget),
        budget=budget,
    )
    work = capture_workspace(
        workspace,
        output / "workspace",
        include=options["include"],
        exclude=options["exclude"],
        writers_stopped=True,
        budget=budget,
    )
    components = {
        "runtime": runtime["manifest_sha256"],
        "native": native["manifest_sha256"],
        "workspace": work["manifest_sha256"],
    }
    if options["reply_directory"] is not None:
        replies = capture_routes(
            options["reply_directory"],
            output / "replies",
            baseline["business"]["native_replies"]["runs"],
            home,
            baseline["binding"]["fingerprint"],
            budget,
        )
        components["replies"] = replies["manifest_sha256"]
    return components

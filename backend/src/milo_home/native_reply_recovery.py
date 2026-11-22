"""Stop the original native family and reconcile evidence without sending any input."""

import sqlite3
from pathlib import Path

from .codex_config import private_read
from .codex_journal import PrivateJournal
from .codex_rpc import ProtocolError
from .errors import ApiError
from .native_messages import NativeMessageEvent, NativeMessageStore
from .native_reply_results import stream_evidence
from .native_reply_route import read_native_route
from .run_transport import unit_empty
from .work_runtime import WorkRuntime
from .work_stop import recorded_identity, stop_family


def reconcile_native_reply(database, base, owner, run, lease):
    """Return False only for a legacy run. Missing native evidence never falls back."""
    try:
        route = read_native_route(base, owner, run["id"])
        if route is None:
            return False
        runtime = WorkRuntime(database, PrivateJournal(Path(route["home"]), initialize=False))
        linked = runtime.journal.business_binding()
        identity = runtime._identity(lease)
        if (
            linked is None
            or linked.owner_id != owner
            or (linked.database, linked.database_device, linked.database_inode) != identity
            or route["fingerprint"] != runtime.journal.fingerprint()
        ):
            raise ProtocolError("Native recovery belongs to another main or database")
        streams = runtime.journal.list_streams(unit=run["unit_name"] + ".service")
        if len(streams) > 1:
            raise ProtocolError("Native recovery has ambiguous execution streams")
        if streams:
            _reconcile_stream(runtime, streams[0]["stream"]["id"], run, lease)
        elif not unit_empty(run["unit_name"]):
            raise ProtocolError("Native execution has no recorded process identity")
        runtime._identity(lease)
        return True
    except (OSError, ValueError, sqlite3.Error, ProtocolError):
        raise ApiError(
            503, "needs_reconciliation", "原生执行或投递证据尚未核对，占用继续保留，不会重发。"
        ) from None


def _reconcile_stream(runtime, stream_id, run, lease):
    journal = runtime.journal
    stream, entries, acknowledged = stream_evidence(journal, stream_id)
    launch = any(
        event.get("kind") == "process" and event.get("phase") == "launch_intent"
        for _, event in entries
    )
    proof_sequence = None
    if launch:
        identity = recorded_identity(entries, run["unit_name"] + ".service")
        if not stop_family(identity):
            raise ProtocolError("Native execution family stop is unconfirmed")
        runtime._identity(lease)
        proof_sequence = journal.record(
            stream_id,
            {"kind": "work_stop", **identity, "checked_at": int(runtime.clock())},
        )
    elif not unit_empty(run["unit_name"]):
        raise ProtocolError("An unrecorded native process is present")
    links = NativeMessageStore(journal)
    saved = links.inspect(run["id"])
    if saved is not None:
        scope, events = saved
        if events:
            first = events[0]
            if first.stream_id != stream_id:
                raise ProtocolError("Native message belongs to another execution stream")
            accepted = next((event for event in events if event.kind == "accepted"), None)
            links.record(
                scope,
                NativeMessageEvent(
                    kind="stopped",
                    stream_id=stream_id,
                    request_id=first.request_id,
                    turn_id=accepted.turn_id if accepted else None,
                    stop_confirmed=True,
                ),
                lease=lease,
            )
    environment = journal.path.parent / (stream_id + ".env")
    if environment.exists() or environment.is_symlink():
        private_read(environment)
        environment.unlink()
    if not acknowledged:
        raise ProtocolError("Stopping cannot resolve unknown native delivery")
    if proof_sequence is not None:
        journal.reconcile_stream(stream_id, proof_sequence)
    elif stream["state"] != "closed" and not journal.close_stream(stream_id, stop=lambda: True):
        raise ProtocolError("Native stream could not be closed")

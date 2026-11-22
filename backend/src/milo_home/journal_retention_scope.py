"""Read-only historical admission for successful, fully delivered native streams.

The caller holds the common host lease. No current grants, schedule, budget,
policy selection, process observation, publication or settlement is consulted.
"""

import os
import re
from dataclasses import asdict

from . import journal_retention_reply, journal_retention_work
from .codex_rpc import ProtocolError
from .journal_retention_facts import RetentionBlocked, digest, one, require
from .native_policy import NativePolicy, NativePolicyStore
from .work_stop import recorded_identity, stop_shape


def historical_policy(journal, entries, linked):
    launches = [
        event
        for _, event in entries
        if event.get("kind") == "process" and event.get("phase") == "launch_intent"
    ]
    require(len(launches) == 1, "launch_identity_incomplete")
    saved = launches[0].get("native_policy")
    if saved is None:
        return None
    store = NativePolicyStore(journal)
    # read() verifies the private marker and current ledger, but does not impose
    # its latest revision on the historical launch being admitted.
    require(store.read() is not None, "historical_policy_missing")
    with store._connect() as connection:
        revisions = connection.execute(
            "SELECT revision,body,digest FROM revisions WHERE revision<=? ORDER BY revision",
            (saved.get("revision"),),
        ).fetchall()
    previous = None
    selected = None
    for expected_revision, row in enumerate(revisions, 1):
        policy = NativePolicy.model_validate_json(row[1])
        require(
            row[0] == policy.revision == expected_revision
            and policy.encoded() == row[1]
            and policy.digest == row[2]
            and policy.previous_digest == previous
            and policy.baseline_fingerprint == journal.fingerprint()
            and str(policy.owner_id) == linked.owner_id
            and str(policy.session_id) == linked.session_id,
            "historical_policy_conflict",
        )
        previous = policy.digest
        selected = policy
    require(selected is not None and selected.summary() == saved, "historical_policy_conflict")
    return {"body": selected.encoded(), "digest": selected.digest}


def _lease_identity(runtime, lease, linked):
    lease.verify()
    metadata = os.fstat(lease.descriptor)
    require(
        linked is not None
        and (linked.database, linked.database_device, linked.database_inode)
        == (str(lease.canonical), metadata.st_dev, metadata.st_ino)
        and runtime.database.path.resolve(strict=True) == lease.canonical
        and getattr(runtime.database, "uid", os.geteuid()) == lease.owner,
        "business_database_identity_conflict",
    )


def _business_identity(connection, linked, binding):
    session = one(connection, "SELECT * FROM work_sessions WHERE id=?", (linked.session_id,))
    stored = one(
        connection, "SELECT * FROM work_runtime_bindings WHERE session_id=?", (linked.session_id,)
    )
    workspace = one(
        connection, "SELECT root_path FROM workspaces WHERE id=?", (linked.workspace_id,)
    )
    require(
        session is not None
        and stored is not None
        and workspace is not None
        and session["owner_id"] == linked.owner_id
        and session["workspace_id"] == linked.workspace_id
        and stored["binding_id"] == linked.binding_id
        and stored["fingerprint"] == linked.fingerprint
        and workspace["root_path"] == binding["cwd"],
        "business_binding_conflict",
    )
    return {"session": session, "registration": stored, "root": workspace["root_path"]}


def _protocol(snapshot, thread_id):
    intents = {}
    responses = {}
    prior_sequence = 0
    for sequence, event in snapshot["entries"]:
        require(type(sequence) is int and sequence > prior_sequence, "entry_sequence_conflict")
        prior_sequence = sequence
        kind = event.get("kind")
        require(kind != "unknown", "unknown_protocol_delivery")
        if kind == "intent":
            request = event["request"]
            identity = request["id"]
            require(identity not in intents, "duplicate_protocol_request")
            intents[identity] = (sequence, request)
        elif kind == "response":
            identity = event["id"]
            require(
                identity in intents
                and identity not in responses
                and event["response"].get("id") == identity
                and sequence > intents[identity][0],
                "protocol_response_conflict",
            )
            responses[identity] = event["response"]
    expected = {
        identity: (request["method"], "acknowledged") for identity, (_, request) in intents.items()
    }
    actual = {row["request_id"]: (row["method"], row["state"]) for row in snapshot["requests"]}
    require(expected == actual and set(responses) == set(intents), "protocol_requests_incomplete")
    starts = []
    turn_ids = set()
    for sequence, request in intents.values():
        require(
            request["method"] not in {"thread/start", "thread/inject_items", "turn/interrupt"},
            "unsupported_historical_execution",
        )
        if request["method"] not in {"turn/start", "turn/steer", "thread/resume"}:
            continue
        require(request.get("params", {}).get("threadId") == thread_id, "protocol_thread_conflict")
        if request["method"] == "turn/start":
            turn = responses[request["id"]].get("result", {}).get("turn", {})
            require(isinstance(turn.get("id"), str), "turn_acceptance_missing")
            require(turn["id"] not in turn_ids, "duplicate_accepted_turn")
            turn_ids.add(turn["id"])
            terminals = [
                (s, e["message"]["params"]["turn"])
                for s, e in snapshot["entries"]
                if e.get("kind") == "notification"
                and e.get("message", {}).get("method") == "turn/completed"
                and e["message"].get("params", {}).get("threadId") == thread_id
                and e["message"]["params"].get("turn", {}).get("id") == turn["id"]
            ]
            require(
                bool(terminals)
                and all(s > sequence and t.get("status") == "completed" for s, t in terminals),
                "successful_turn_terminal_missing",
            )
            starts.append(request["id"])
    require(bool(starts), "no_successful_model_execution")
    for _, event in snapshot["entries"]:
        if event.get("kind") != "notification":
            continue
        message = event.get("message", {})
        if message.get("method") != "turn/completed":
            continue
        params = message.get("params", {})
        require(
            params.get("threadId") == thread_id and params.get("turn", {}).get("id") in turn_ids,
            "unmapped_terminal_execution",
        )
    return starts


def _stopping(snapshot):
    entries = snapshot["entries"]
    unit = snapshot["stream"]["unit"]
    identity = recorded_identity(entries, unit)
    ready_sequence = next(
        s for s, e in entries if e.get("kind") == "process" and e.get("phase") == "ready"
    )
    launch_sequences = [
        s for s, e in entries if e.get("kind") == "process" and e.get("phase") == "launch_intent"
    ]
    execution_sequences = [
        s for s, e in entries if e.get("kind") in {"intent", "response", "notification"}
    ]
    require(
        len(launch_sequences) == 1
        and launch_sequences[0] < ready_sequence
        and execution_sequences
        and ready_sequence < min(execution_sequences),
        "process_execution_order_conflict",
    )
    last_execution = max(
        (
            s
            for s, e in entries
            if e.get("kind")
            in {"intent", "response", "notification", "work_terminal", "work_receipt_repair"}
        ),
        default=ready_sequence,
    )
    stops = []
    unknown = 0
    for sequence, event in entries:
        if event.get("kind") == "process":
            require(event.get("unit") == unit, "process_unit_conflict")
            if event.get("phase") == "stop_unknown":
                unknown = sequence
            if event.get("phase") == "stop_confirmed":
                stops.append((sequence, event))
        elif event.get("kind") == "work_stop":
            stop_shape(event)
            require({key: event[key] for key in identity} == identity, "stop_identity_conflict")
            stops.append((sequence, event))
    require(bool(stops), "historical_stop_missing")
    sequence, event = stops[-1]
    require(sequence > max(last_execution, unknown, ready_sequence), "historical_stop_incomplete")
    return {
        "process_identity": identity,
        "stop_sequence": sequence,
        "stop": event,
        "fresh_observation": False,
    }


def inspect_stream(runtime, stream_id, *, lease, reply_directory=None):
    """Report historical admission; malformed evidence blocks without exposing payloads."""
    report = {
        "eligible": False,
        "blocked_by": [],
        "business_digest": None,
        "scope": {"stream_id": stream_id},
    }
    try:
        require(
            isinstance(stream_id, str) and re.fullmatch(r"[0-9a-f]{32}", stream_id),
            "stream_identity_invalid",
        )
        linked = runtime.journal.business_binding()
        _lease_identity(runtime, lease, linked)
        binding = runtime.journal.binding_record()
        require(linked.fingerprint == runtime.journal.fingerprint(), "native_binding_conflict")
        snapshot = runtime.journal.stream_snapshot(stream_id)
        report["scope"]["unit"] = snapshot["stream"]["unit"]
        require(snapshot["stream"]["state"] == "closed", "stream_not_closed")
        require(
            all(row["state"] == "acknowledged" for row in snapshot["requests"]),
            "protocol_requests_incomplete",
        )
        credential = runtime.journal.path.parent / (stream_id + ".env")
        require(
            not credential.exists() and not credential.is_symlink(), "temporary_credentials_present"
        )
        inspector = (
            journal_retention_work if snapshot["work"] is not None else journal_retention_reply
        )
        with runtime.database.connect() as connection:
            connection.execute("BEGIN")
            identity = _business_identity(connection, linked, binding)
            business = inspector.read_business(connection, snapshot)
        # ServiceDatabase has now restored root; only now read private ledgers.
        starts = _protocol(snapshot, binding["thread_id"])
        stopping = _stopping(snapshot)
        report["scope"].update(stopping)
        policy = historical_policy(runtime.journal, snapshot["entries"], linked)
        details = inspector.validate(
            runtime, snapshot, business, linked, starts, policy, reply_directory=reply_directory
        )
        report["scope"].update(details)
        report["scope"]["binding_fingerprint"] = linked.fingerprint
        report["business_digest"] = digest(
            {
                "binding": asdict(linked),
                "identity": identity,
                "business": business,
                "policy": policy,
                "scope": details,
            }
        )
        report["scope"]["evidence_digest"] = digest(snapshot)
        _lease_identity(runtime, lease, linked)
        report["eligible"] = True
    except RetentionBlocked as error:
        report["blocked_by"].append(error.reason)
    except (ProtocolError, ValueError, KeyError, TypeError, OSError, AttributeError):
        report["blocked_by"].append("historical_evidence_invalid")
    return report

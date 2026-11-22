"""Historical success, native routing and actual publication for an ordinary reply."""

import json
from pathlib import Path
from uuid import UUID

from .journal_retention_facts import digest, one, require, rows
from .native_messages import NativeMessageStore
from .native_reply_results import _require_protocol, final_result, result_digest
from .native_reply_route import read_native_route
from .reply_projection_evidence import read_projection, require_publication
from .reply_results import parse_result, require_scope
from .revisit_claims import context


def _run_id(snapshot):
    unit = snapshot["stream"]["unit"]
    require(
        unit.startswith("milo-reply-") and unit.endswith(".service"), "unsupported_stream_family"
    )
    run_id = str(UUID(unit[len("milo-reply-") : -len(".service")]))
    require(unit == "milo-reply-" + run_id + ".service", "reply_unit_identity_conflict")
    return run_id


def read_business(connection, snapshot):
    run_id = _run_id(snapshot)
    run = one(connection, "SELECT * FROM runs WHERE id=?", (run_id,))
    require(run is not None, "reply_run_missing")
    selected = rows(
        connection,
        "SELECT m.* FROM messages m JOIN run_messages s ON s.message_id=m.id "
        "WHERE s.run_id=? ORDER BY m.sequence",
        (run_id,),
    )
    facts = {
        "run": run,
        "owner": one(connection, "SELECT * FROM reply_run_scopes WHERE run_id=?", (run_id,)),
        "thread": one(connection, "SELECT * FROM threads WHERE id=?", (run["thread_id"],)),
        "conversation": one(
            connection,
            "SELECT id,thread_id,kind FROM conversations WHERE id=?",
            (run["conversation_id"],),
        ),
        "selection": rows(
            connection, "SELECT * FROM run_messages WHERE run_id=? ORDER BY message_id", (run_id,)
        ),
        "selected": selected,
        "sealed": one(
            connection, "SELECT * FROM reply_execution_results WHERE run_id=?", (run_id,)
        ),
        "published": rows(
            connection, "SELECT * FROM messages WHERE run_id=? ORDER BY in_reply_to", (run_id,)
        ),
        "dispatch": one(connection, "SELECT * FROM wake_dispatches WHERE run_id=?", (run_id,)),
        "launch": one(connection, "SELECT * FROM legacy_launches WHERE run_id=?", (run_id,)),
        "events": rows(
            connection,
            "SELECT e.* FROM wake_events e JOIN wake_run_events r ON r.event_id=e.id "
            "WHERE r.run_id=? ORDER BY e.id",
            (run_id,),
        ),
        "event_links": rows(
            connection,
            "SELECT * FROM wake_run_events WHERE run_id=? ORDER BY event_id",
            (run_id,),
        ),
        "event_delivery": rows(
            connection,
            "SELECT d.* FROM wake_event_delivery d JOIN wake_run_events r ON r.event_id=d.event_id "
            "WHERE r.run_id=? ORDER BY d.event_id",
            (run_id,),
        ),
        "still_reserved": rows(
            connection,
            "SELECT * FROM wake_event_delivery WHERE run_id=? ORDER BY event_id",
            (run_id,),
        ),
    }
    projection = read_projection(connection, run_id)
    if any(projection.values()):
        facts["decision_projection"] = projection
    return facts


def _business(facts, linked):
    run, owner, thread, conversation = (
        facts[key] for key in ("run", "owner", "thread", "conversation")
    )
    require(
        owner is not None
        and thread is not None
        and conversation is not None
        and owner["owner_id"] == thread["owner_id"] == linked.owner_id
        and run["thread_id"] == thread["id"] == conversation["thread_id"]
        and run["conversation_id"] == conversation["id"]
        and run["unit_name"] + ".service" == "milo-reply-" + run["id"] + ".service",
        "reply_business_scope_conflict",
    )
    require(
        run["status"] == "succeeded" and run["ended_at"] is not None, "reply_publication_incomplete"
    )
    require(facts["sealed"] is not None, "reply_sealed_result_missing")
    dispatch, launch = facts["dispatch"], facts["launch"]
    require(
        dispatch is not None
        and launch is not None
        and dispatch["settled_at"] is not None
        and dispatch["launched_at"] is not None
        and launch["authorized_at"] is not None
        and not facts["still_reserved"]
        and run["created_at"] <= launch["authorized_at"] <= run["ended_at"]
        and run["created_at"] <= dispatch["launched_at"] <= run["ended_at"]
        and dispatch["settled_at"] >= run["ended_at"],
        "reply_dispatch_unsettled",
    )
    ids = {row["id"] for row in facts["selected"]}
    revisit = facts.get("decision_projection", {}).get("revisit_claim")
    require(
        (0 if revisit else 1) <= len(ids) <= 10
        and len(ids) == len(facts["selected"]) == len(facts["selection"])
        and {row["message_id"] for row in facts["selection"]} == ids
        and all(row["thread_id"] == run["thread_id"] for row in facts["selection"])
        and all(
            row["thread_id"] == run["thread_id"]
            and row["conversation_id"] == run["conversation_id"]
            and row["author_id"] == linked.owner_id
            and row["role"] == "visitor"
            for row in facts["selected"]
        ),
        "reply_selected_messages_conflict",
    )
    require(
        (bool(facts["events"]) or revisit is not None)
        and len(facts["events"]) == len(facts["event_links"])
        and {event["id"] for event in facts["events"]}
        == {row["event_id"] for row in facts["event_links"]}
        == {row["event_id"] for row in facts["event_delivery"]}
        and {event["message_id"] for event in facts["events"] if event["kind"] == "message"} == ids
        and all(
            event["state"] == "done"
            and event["thread_id"] == run["thread_id"]
            and (
                event["kind"] == "message"
                or event["kind"] == "fallback"
                and event["message_id"] is None
            )
            for event in facts["events"]
        )
        and all(row["run_id"] is None for row in facts["event_delivery"]),
        "reply_dispatch_events_unsettled",
    )


def _native(runtime, snapshot, facts, linked, starts, policy):
    saved = NativeMessageStore(runtime.journal).inspect(facts["run"]["id"])
    require(saved is not None, "native_message_scope_missing")
    scope, events = saved
    require(
        str(scope.owner_id) == linked.owner_id
        and str(scope.business_binding_id) == linked.binding_id
        and str(scope.conversation_id) == facts["run"]["conversation_id"]
        and set(map(str, scope.message_ids)) == {row["id"] for row in facts["selected"]}
        and policy is not None
        and policy["digest"] == scope.policy_digest,
        "native_reply_scope_conflict",
    )
    expected_revisit = context(facts.get("decision_projection", {}).get("revisit_claim"))
    require(
        (scope.revisit.model_dump(mode="json") if scope.revisit else None) == expected_revisit,
        "native_reply_scope_conflict",
    )
    accepted = next((event for event in events if event.kind == "accepted"), None)
    terminal = next((event for event in events if event.kind == "terminal"), None)
    require(
        accepted is not None
        and terminal is not None
        and len(starts) == 1
        and accepted.request_id == starts[0]
        and all(event.stream_id == snapshot["stream"]["id"] for event in events)
        and terminal.outcome == "completed"
        and terminal.result_digest is not None
        and events[-1].kind == "stopped"
        and events[-1].stop_confirmed is True,
        "native_reply_terminal_incomplete",
    )
    _require_protocol(snapshot["entries"], scope, accepted)
    sealed = facts["sealed"]
    result = parse_result(sealed["result"])
    require_scope(
        result,
        [str(identity) for identity in scope.message_ids],
        sealed["completed_at"],
        revisit_source_ids=scope.revisit.source_ids if scope.revisit else (),
    )
    require(
        result.model_dump_json() == sealed["result"]
        and result_digest(result) == terminal.result_digest
        and final_result(runtime.journal, snapshot["stream"]["id"], scope, accepted.turn_id)
        == result
        and sealed["completed_at"] == facts["run"]["ended_at"]
        and facts["run"]["created_at"] <= sealed["completed_at"] < facts["run"]["deadline_at"],
        "native_reply_result_conflict",
    )
    original = next(
        event
        for _, event in snapshot["entries"]
        if event.get("kind") == "intent" and event["request"]["id"] == accepted.request_id
    )
    require(original.get("reply") == scope.model_dump(mode="json"), "native_reply_launch_conflict")
    ready = next(
        event
        for _, event in snapshot["entries"]
        if event.get("kind") == "process" and event.get("phase") == "ready"
    )
    require(sealed["boot_id"] == ready["boot_id"], "native_reply_completion_boot_conflict")
    require(
        all(
            event["request"]["method"] != "turn/steer"
            for _, event in snapshot["entries"]
            if event.get("kind") == "intent"
        ),
        "native_reply_unexpected_steer",
    )
    request = original["request"]
    payload = json.loads(request["params"]["input"][0]["text"].rsplit("\n", 1)[-1])
    _input(payload, facts, scope)
    try:
        require_publication(
            result,
            facts["run"],
            facts["published"],
            facts.get(
                "decision_projection",
                {name: [] for name in ("decisions", "coverage", "revisits", "sources")},
            ),
            [str(identity) for identity in scope.message_ids],
            sealed["completed_at"],
        )
    except ValueError:
        require(False, "reply_message_projection_conflict")
    launches = [
        event
        for _, event in snapshot["entries"]
        if event.get("kind") == "process" and event.get("phase") == "launch_intent"
    ]
    require(launches[0].get("reply_run_id") == str(scope.run_id), "native_reply_launch_conflict")
    return scope, events


def _input(payload, facts, scope):
    expected = [
        {key: row[key] for key in ("id", "role", "body", "sequence", "created_at", "in_reply_to")}
        for row in facts["selected"]
    ]
    require(
        payload.get("source")
        == (
            "saved_owner_conversation_revisit"
            if scope.revisit
            else "authenticated_owner_website_messages"
        )
        and payload["owner_id"] == str(scope.owner_id)
        and payload["run_id"] == str(scope.run_id)
        and payload["conversation_id"] == str(scope.conversation_id)
        and payload["created_at"] == facts["run"]["created_at"]
        and payload["deadline_at"] == facts["run"]["deadline_at"]
        and payload["selected"] == expected,
        "native_reply_input_conflict",
    )
    require(
        payload.get("revisit")
        == (scope.revisit.model_dump(mode="json") if scope.revisit else None),
        "native_reply_input_conflict",
    )


def validate(runtime, snapshot, facts, linked, starts, policy, *, reply_directory=None):
    _business(facts, linked)
    scope, events = _native(runtime, snapshot, facts, linked, starts, policy)
    require(reply_directory is not None, "native_route_unavailable")
    directory = Path(reply_directory)
    require(
        directory.is_absolute() and directory.resolve(strict=True) == directory,
        "native_route_unavailable",
    )
    route = read_native_route(directory / str(scope.run_id), linked.owner_id, str(scope.run_id))
    require(
        route is not None
        and route["home"] == str(runtime.journal.path.parent)
        and route["fingerprint"] == linked.fingerprint,
        "native_route_conflict",
    )
    return {
        "kind": "native_reply",
        "run_id": str(scope.run_id),
        "conversation_id": str(scope.conversation_id),
        "input_digest": scope.input_digest,
        "native_scope_digest": scope.digest,
        "native_events_digest": digest([e.model_dump(mode="json") for e in events]),
        "route_digest": digest(route),
        "policy_digest": scope.policy_digest,
        "published_messages": len(facts["published"]),
    }

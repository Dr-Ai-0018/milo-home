"""Atomically publish one natural answer per group and explicit non-answer decisions."""

from uuid import uuid4

from .reception_results import publish_reception
from .reply_choice_result import DeferChoice, ReplyChoice
from .reply_results import require_scope
from .revisit_claims import claim_current, read_claim
from .revisit_publication import choices_for_publication, complete


def _revisit(connection, run, request, index, completed_at, now):
    owner = connection.execute(
        "SELECT owner_id FROM threads WHERE id=?", (run["thread_id"],)
    ).fetchone()[0]
    identity = str(uuid4())
    connection.execute(
        "INSERT INTO revisit_intents (id,owner_id,conversation_id,origin_run_id,origin_index,"
        "state,due_at,initial_due_at,created_at,updated_at) VALUES (?,?,?,?,?,'scheduled',?,?,?,?)",
        (
            identity,
            owner,
            run["conversation_id"],
            run["id"],
            index,
            request.revisit_at,
            request.revisit_at,
            completed_at,
            now,
        ),
    )
    connection.executemany(
        "INSERT INTO revisit_sources VALUES (?,?)",
        [(identity, str(value)) for value in request.message_ids],
    )
    return identity


def _reply(connection, run, choice, sequence, now):
    anchor = str(choice.message_ids[0])
    if connection.execute("SELECT 1 FROM messages WHERE in_reply_to=?", (anchor,)).fetchone():
        # The legacy column is unique. Continuations anchor to the last answer;
        # canonical coverage remains the incoming identities in message_reply_links.
        anchor = connection.execute(
            "SELECT id FROM messages WHERE conversation_id=? AND role='milo' "
            "ORDER BY sequence DESC LIMIT 1",
            (run["conversation_id"],),
        ).fetchone()[0]
    identity = str(uuid4())
    connection.execute(
        "INSERT INTO messages (id,thread_id,sequence,role,body,in_reply_to,run_id,created_at,"
        "conversation_id) VALUES (?,?,?,'milo',?,?,?,?,?)",
        (
            identity,
            run["thread_id"],
            sequence,
            choice.body,
            anchor,
            run["id"],
            now,
            run["conversation_id"],
        ),
    )
    connection.executemany(
        "INSERT INTO message_reply_links VALUES (?,?,?)",
        [(identity, str(value), run["id"]) for value in choice.message_ids],
    )
    return identity


def publish_choices(connection, run, result, now, *, completed_at):
    if not connection.in_transaction:
        raise ValueError("Reply decisions require the original publication transaction")
    ids = [
        row[0]
        for row in connection.execute(
            "SELECT message_id FROM run_messages WHERE run_id=?", (run["id"],)
        )
    ]
    saved = read_claim(connection, run["id"])
    sources = [row["message_id"] for row in saved["sources"]] if saved else []
    require_scope(result, ids, completed_at, revisit_source_ids=sources)
    if saved and not claim_current(connection, run["id"]):
        raise ValueError("Revisit changed before publication")
    if result.reception is not None:
        publish_reception(connection, run, result.reception, now, completed_at)
    placeholders = ",".join("?" for _ in ids)
    states = connection.execute(
        f"SELECT state FROM message_reply_state WHERE message_id IN ({placeholders})", ids
    ).fetchall()
    if len(states) != len(ids) or any(row[0] not in {"unprocessed", "unknown"} for row in states):
        raise ValueError("Selected messages already have a persisted disposition")
    sequence = connection.execute(
        "SELECT coalesce(max(sequence),0) FROM messages WHERE thread_id=?", (run["thread_id"],)
    ).fetchone()[0]
    revisits = {}
    for index, choice in enumerate([*result.decisions, *result.revisits]):
        if isinstance(choice, DeferChoice) or index >= len(result.decisions):
            identity = _revisit(connection, run, choice, index, completed_at, now)
            revisits.update({str(value): identity for value in choice.message_ids})
    for choice in choices_for_publication(result, saved):
        reply_id = None
        if isinstance(choice, ReplyChoice):
            sequence += 1
            reply_id = _reply(connection, run, choice, sequence, now)
        state = {"reply": "replied", "no_reply": "no_reply", "defer": "deferred"}[choice.kind]
        for value in choice.message_ids:
            message_id = str(value)
            revision = connection.execute(
                "SELECT coalesce(max(revision),0)+1 FROM message_decision_history "
                "WHERE message_id=?",
                (message_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO message_decisions VALUES (?,?,?,?,?,?,?)",
                (
                    message_id,
                    revision,
                    run["id"],
                    state,
                    completed_at,
                    reply_id,
                    revisits.get(message_id),
                ),
            )
    complete(connection, saved, now)

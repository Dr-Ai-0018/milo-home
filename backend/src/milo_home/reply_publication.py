"""Atomic projection of a checked, frozen reply batch into its original conversation."""

from uuid import uuid4

from .reply_choice_publication import publish_choices
from .reply_choice_result import ReplyChoiceResult


def publish_rows(connection, run, result, now, *, completed_at=None):
    if isinstance(result, ReplyChoiceResult):
        completed_at = now if completed_at is None else completed_at
        publish_choices(connection, run, result, now, completed_at=completed_at)
        connection.execute(
            "UPDATE runs SET status='succeeded',ended_at=? WHERE id=?",
            (completed_at, run["id"]),
        )
        return
    ids = [
        row[0]
        for row in connection.execute(
            "SELECT message_id FROM run_messages WHERE run_id=?", (run["id"],)
        )
    ]
    result.require_scope(ids)
    sequence = connection.execute(
        "SELECT coalesce(max(sequence),0) FROM messages WHERE thread_id=?", (run["thread_id"],)
    ).fetchone()[0]
    for index, reply in enumerate(result.replies, 1):
        connection.execute(
            "INSERT INTO messages (id,thread_id,sequence,role,body,in_reply_to,run_id,"
            "created_at,conversation_id) VALUES (?,?,?,'milo',?,?,?,?,?)",
            (
                str(uuid4()),
                run["thread_id"],
                sequence + index,
                reply.body,
                str(reply.message_id),
                run["id"],
                now,
                run["conversation_id"],
            ),
        )
    connection.execute(
        "UPDATE runs SET status='succeeded',ended_at=? WHERE id=?",
        (now if completed_at is None else completed_at, run["id"]),
    )

"""Project persisted choices and coverage without inventing decisions for old rows."""

import time

from .interaction_models import ReplySummary
from .interaction_timing import RevisitSummary
from .revisit_queue import observed


def revisit_views(connection, identities):
    if not identities:
        return {}
    placeholders = ",".join("?" for _ in identities)
    rows = connection.execute(
        f"SELECT * FROM revisit_intents WHERE id IN ({placeholders})",
        list(identities),
    )
    now = int(time.time())
    fields = ("id", "revision", "state", "due_at", "updated_at", "blocked_reason")
    return {
        row["id"]: {key: value[key] for key in fields}
        for row in rows
        if (value := observed(connection, row, now))
    }


def message_views(connection, rows):
    messages = [dict(row) for row in rows]
    if not messages:
        return messages
    identities = [row["id"] for row in messages]
    placeholders = ",".join("?" for _ in identities)
    states = {
        row["message_id"]: row
        for row in connection.execute(
            f"SELECT * FROM message_reply_state WHERE message_id IN ({placeholders})", identities
        )
    }
    revisits = revisit_views(
        connection, {row["revisit_id"] for row in states.values() if row["revisit_id"]}
    )
    coverage = {}
    for row in connection.execute(
        "SELECT reply_id,message_id FROM message_reply_links "
        f"WHERE reply_id IN ({placeholders}) ORDER BY message_id",
        identities,
    ):
        coverage.setdefault(row["reply_id"], []).append(row["message_id"])
    for message in messages:
        message["reply_decision"] = None
        message["reply_to_ids"] = coverage.get(message["id"])
        state = states.get(message["id"])
        if state is None or state["state"] == "unknown":
            continue
        if state["revision"] is None and state["state"] != "unprocessed":
            continue
        message["reply_decision"] = {
            "state": state["state"],
            "revision": state["revision"],
            "decided_at": state["decided_at"],
            "reply_id": state["reply_id"],
            "revisit": revisits.get(state["revisit_id"]),
        }
    return messages


def conversation_views(connection, rows):
    conversations = [dict(row) for row in rows]
    if not conversations:
        return conversations
    placeholders = ",".join("?" for _ in conversations)
    grouped = {}
    for row in connection.execute(
        "SELECT conversation_id,state,count(*) AS count FROM message_reply_state "
        f"WHERE conversation_id IN ({placeholders}) GROUP BY conversation_id,state",
        [row["id"] for row in conversations],
    ):
        grouped.setdefault(row["conversation_id"], {})[row["state"] + "_count"] = row["count"]
    for conversation in conversations:
        summary = reply_summary(grouped.get(conversation["id"], {}))
        conversation["reply_summary"] = summary.model_dump()
    return conversations


def reply_summary(counts):
    return ReplySummary(
        **{
            name + "_count": counts.get(name + "_count", 0)
            for name in ("unprocessed", "replied", "no_reply", "deferred", "unknown")
        }
    )


def conversation_summary(connection, conversation_id):
    counts = {
        row["state"] + "_count": row["count"]
        for row in connection.execute(
            "SELECT state,count(*) AS count FROM message_reply_state "
            "WHERE conversation_id=? GROUP BY state",
            (conversation_id,),
        )
    }
    return reply_summary(counts)


def revisit_summary(connection, conversation_id, now):
    rows = [
        observed(connection, row, now)
        for row in connection.execute(
            "SELECT * FROM revisit_intents WHERE conversation_id=? "
            "AND state NOT IN ('completed','cancelled')",
            (conversation_id,),
        )
    ]
    return RevisitSummary(
        pending_count=len(rows),
        ready_count=sum(row["state"] == "scheduled" and row["due_at"] <= now for row in rows),
        active_count=sum(row["state"] == "running" for row in rows),
        blocked_count=sum(row["state"] == "blocked" for row in rows),
        reconciliation_count=sum(row["state"] == "needs_reconciliation" for row in rows),
        next_due_at=min((row["due_at"] for row in rows), default=None),
    )

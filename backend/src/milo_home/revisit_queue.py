"""Due revisits share the ordinary queue without resetting original message delivery."""

from .automatic_services import entry_available, service_database_matches
from .execution_budget import shared_budget_view, shared_policy
from .execution_control import execution_busy, working
from .management_lease import current_boot
from .reception import blocking_reason
from .revisit_claims import sources


def enabled(connection, owner, now):
    row = connection.execute(
        "SELECT * FROM reply_revisit_capabilities WHERE owner_id=?", (owner,)
    ).fetchone()
    return bool(row and row["enabled"] and row["updated_at"] <= now)


def blocked_by(connection, intent, now):
    if intent["updated_at"] > now or not working(connection, now):
        return "schedule"
    if blocking_reason(connection, intent["owner_id"], "reply", now):
        return "reception"
    policy = shared_policy(connection)
    if policy is None or shared_budget_view(connection, policy, now, 1)["blocking_reason"]:
        return "budget"
    service = connection.execute(
        "SELECT * FROM automatic_services WHERE kind='reply' AND owner_id=?", (intent["owner_id"],)
    ).fetchone()
    if (
        not enabled(connection, intent["owner_id"], now)
        or not service_database_matches(connection, service)
        or not entry_available(service, now, current_boot())
    ):
        return "service"
    if execution_busy(connection):
        return "occupancy"
    return None


def observed(connection, row, now):
    result = dict(row)
    if row["state"] in {"scheduled", "blocked"} and row["due_at"] <= now:
        reason = blocked_by(connection, row, now)
        result.update(state="blocked" if reason else "scheduled", blocked_reason=reason)
    return result


def candidate_key(candidate):
    return candidate.get("key") or candidate["message_ids"][0]


def with_due(connection, owner, now, candidates):
    if not enabled(connection, owner, now):
        return candidates
    due = connection.execute(
        "SELECT * FROM revisit_intents WHERE owner_id=? AND due_at<=? "
        "AND state IN ('scheduled','blocked') ORDER BY due_at,id LIMIT 1000",
        (owner, now),
    ).fetchall()
    result = list(candidates)
    for intent in due:
        reason = blocked_by(connection, intent, now)
        if (
            intent["state"] != ("blocked" if reason else "scheduled")
            or intent["blocked_reason"] != reason
        ):
            connection.execute(
                "UPDATE revisit_intents SET state=?,blocked_reason=?,updated_at=? WHERE id=?",
                ("blocked" if reason else "scheduled", reason, now, intent["id"]),
            )
        if reason:
            continue
        original = sources(connection, intent["id"])
        if not 1 <= len(original) <= 10:
            raise ValueError("Revisit has missing or excessive source identities")
        capacity = 10 - len(original)
        incoming = next(
            (
                row
                for row in result
                if row["conversation_id"] == intent["conversation_id"]
                and row["ready_at"] <= now
                and "revisit" not in row
            ),
            None,
        )
        selected, events = [], []
        if incoming and capacity:
            result.remove(incoming)
            selected = incoming["message_ids"][:capacity]
            events = (
                incoming["event_ids"][: len(selected)]
                + incoming["event_ids"][len(incoming["message_ids"]) :]
            )
        thread = connection.execute(
            "SELECT thread_id FROM conversations WHERE id=?", (intent["conversation_id"],)
        ).fetchone()[0]
        result.append(
            {
                "thread_id": thread,
                "conversation_id": intent["conversation_id"],
                "message_ids": selected,
                "event_ids": events,
                "ready_at": min(intent["due_at"], incoming["ready_at"])
                if selected
                else intent["due_at"],
                "cause": "revisit",
                "key": "revisit:" + intent["id"],
                "revisit": {
                    "id": intent["id"],
                    "revision": intent["revision"],
                    "due_at": intent["due_at"],
                },
            }
        )
    return sorted(result, key=lambda row: (row["ready_at"], candidate_key(row)))


def settle(connection, now):
    rows = connection.execute(
        "SELECT c.*,r.status,r.failure_code,d.launched_at,d.abort_reason "
        "FROM revisit_claims c JOIN runs r ON r.id=c.run_id "
        "JOIN wake_dispatches d ON d.run_id=r.id "
        "JOIN revisit_intents v ON v.id=c.revisit_id AND v.revision=c.revision "
        "WHERE v.state='running' AND r.status IN ('succeeded','cancelled','failed','timed_out')"
    ).fetchall()
    for row in rows:
        if row["status"] == "succeeded":
            state, revision, due = "completed", row["revision"], row["due_at"]
        elif row["launched_at"] is None and row["abort_reason"] in {
            "reception_closed",
            "reception_clock_invalid",
            "schedule_closed",
            "lease_expired",
            "clock_rewind",
        }:
            state, revision, due = "scheduled", row["revision"] + 1, max(now + 30, row["due_at"])
        else:
            state, revision, due = "needs_reconciliation", row["revision"], row["due_at"]
        connection.execute(
            "UPDATE revisit_intents SET state=?,revision=?,due_at=?,updated_at=?,"
            "blocked_reason=NULL "
            "WHERE id=? AND revision=?",
            (state, revision, due, now, row["revisit_id"], row["revision"]),
        )


def mark_uncertain(connection, owner, now):
    connection.execute(
        "UPDATE revisit_intents SET state='needs_reconciliation',updated_at=? "
        "WHERE owner_id=? AND state='running' AND EXISTS "
        "(SELECT 1 FROM revisit_claims c JOIN runs r ON r.id=c.run_id "
        "WHERE c.revisit_id=revisit_intents.id AND c.revision=revisit_intents.revision "
        "AND r.status IN ('queued','running','stopping') AND NOT EXISTS "
        "(SELECT 1 FROM reply_execution_results s WHERE s.run_id=r.id))",
        (now, owner),
    )

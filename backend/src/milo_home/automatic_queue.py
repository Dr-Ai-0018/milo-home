"""One stable choice under the host lock; losing an election reserves nothing."""

from .automatic_services import entry_available, require_entry, service_database_matches
from .errors import ApiError
from .reception import blocking_reason
from .reply_scope import ReplyScope, queue_counts
from .revisit_queue import candidate_key
from .wakeup_events import EventPlanner
from .work_access import require_access, require_session
from .work_queue import decisions_enabled, next_work


def work_candidate(connection, entry, now):
    try:
        session = require_session(connection, entry["owner_id"], entry["session_id"])
        access = require_access(connection, entry["owner_id"], session["workspace_id"])
    except ApiError:
        return None
    if blocking_reason(connection, entry["owner_id"], "work", now):
        return None
    row = next_work(
        connection,
        entry["owner_id"],
        entry["session_id"],
        access,
        now,
        decisions=decisions_enabled(connection, entry["owner_id"]),
    )
    if row:
        return {"kind": "work", "ready_at": row["ready_at"], "key": row["id"]}
    return None


def automatic_choice(connection, kind, owner, now, boot, *, session=None, reply_candidates=None):
    if not connection.in_transaction:
        raise ValueError("Automatic selection requires an existing transaction")
    entries = {row["kind"]: row for row in connection.execute("SELECT * FROM automatic_services")}
    if kind == "work" and kind not in entries:
        # An upgrade does not silently put the existing work service under new control.
        return {"kind": "work"}
    own = require_entry(connection, kind, owner)
    if kind == "work" and own["session_id"] != session:
        raise ApiError(409, "service_scope_changed", "自动交办主会话与登记不符。")
    if not entry_available(own, now, boot, caller=True):
        return None
    choices = []
    for candidate_kind, entry in entries.items():
        if not service_database_matches(connection, entry):
            continue
        if not entry_available(entry, now, boot, caller=candidate_kind == kind):
            continue
        if candidate_kind == "work":
            candidate = work_candidate(connection, entry, now)
            if candidate and candidate["ready_at"] <= now:
                choices.append(candidate)
        else:
            scope = ReplyScope(entry["owner_id"])
            if queue_counts(connection, scope.thread_id(connection), now).clock_invalid:
                continue
            if reply_candidates is None:
                planner = EventPlanner(None, scope=scope, include_revisits=True)
                candidates = planner.plan_in_transaction(connection, now)["candidates"]
            else:
                candidates = reply_candidates
            choices.extend(
                {"kind": "reply", "ready_at": row["ready_at"], "key": candidate_key(row)}
                for row in candidates
                if row["ready_at"] <= now
            )
    return (
        min(choices, key=lambda row: (row["ready_at"], row["kind"], row["key"]))
        if choices
        else None
    )

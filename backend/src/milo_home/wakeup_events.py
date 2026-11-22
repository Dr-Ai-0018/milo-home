"""Persistent event discovery; planning never starts or claims an execution."""

import time
from datetime import datetime, timedelta
from uuid import uuid4

from .database import Database
from .knock_delivery import pending_knock
from .reception import blocking_reason, settle_due
from .revisit_queue import with_due
from .schedule_models import minute
from .schedule_policy import BEIJING, day_start, phase_at
from .schedules import schedule_snapshot

DEBOUNCE_SECONDS = 15
MAX_WAIT_SECONDS = 60
ALARM_GRACE_SECONDS = 60
SCAN_LIMIT = 1000
MAX_RESERVATIONS = 3
ELIGIBLE = (
    "wake_events.kind = 'message' AND wake_events.state = 'pending' "
    "AND EXISTS (SELECT 1 FROM message_reply_state AS choice "
    "WHERE choice.message_id=wake_events.message_id AND choice.state IN ('unprocessed','unknown')) "
    "AND delivery.run_id IS NULL AND delivery.blocked_reason IS NULL "
    "AND coalesce(delivery.retry_after, 0) <= ?"
    " AND (coalesce(delivery.attempts, 0) < 3 OR EXISTS "
    "(SELECT 1 FROM reception_returned_attempts WHERE event_id=wake_events.id))"
)
DELIVERY_JOIN = "LEFT JOIN wake_event_delivery AS delivery ON delivery.event_id = wake_events.id "


def alarm_times(policy, state, lower: int, now: int):
    """At most 61 seconds are examined, including a possible Beijing midnight."""
    date_ = datetime.fromtimestamp(lower, BEIJING).date()
    end = datetime.fromtimestamp(now, BEIJING).date()
    while date_ <= end:
        plan, _ = policy.day(date_)
        for alarm in plan.fallback:
            timestamp = day_start(date_) + minute(alarm) * 60
            if lower < timestamp <= now and phase_at(policy, state, timestamp)[0] == "working":
                yield timestamp
        date_ += timedelta(days=1)


class EventPlanner:
    def __init__(self, database: Database, clock=time.time, *, scope=None, include_revisits=False):
        self.database = database
        self.clock = clock
        self.scope = scope
        self.include_revisits = include_revisits

    def status(self):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            cursor = dict(
                connection.execute("SELECT * FROM wake_event_cursor WHERE id = 1").fetchone()
            )
            counts = [
                dict(row)
                for row in connection.execute(
                    "SELECT kind, state, count(*) AS count FROM wake_events GROUP BY kind, state"
                )
            ]
            deliveries = [
                dict(row)
                for row in connection.execute(
                    "SELECT CASE WHEN blocked_reason IS NOT NULL THEN 'blocked' "
                    "WHEN run_id IS NOT NULL THEN 'reserved' "
                    "WHEN retry_after > ? THEN 'retry_wait' ELSE 'ready' END AS state, "
                    "count(*) AS count FROM wake_event_delivery "
                    "JOIN wake_events ON wake_events.id = wake_event_delivery.event_id "
                    "WHERE wake_events.kind = 'message' AND wake_events.state = 'pending' "
                    "GROUP BY 1",
                    (int(self.clock()),),
                )
            ]
        return {
            "automatic_wakeup": False,
            "cursor": cursor,
            "counts": counts,
            "deliveries": deliveries,
        }

    def plan(self):
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            return self.plan_in_transaction(connection, now)

    def plan_in_transaction(self, connection, now):
        if not connection.in_transaction:
            raise ValueError("Event planning requires an existing transaction")
        thread_id = self.scope.thread_id(connection) if self.scope else None
        settle_due(connection, now, owner=self.scope.owner_id if self.scope else None)
        policy, state = schedule_snapshot(connection)
        cursor = connection.execute("SELECT * FROM wake_event_cursor WHERE id = 1").fetchone()
        previous = cursor["last_scan_at"]
        if previous is not None and now < previous:
            return self._view(now, "clock_rewind", 0, [])
        if state["paused"]:
            phase = "paused"
        elif policy:
            phase = phase_at(policy, state, now)[0]
        else:
            phase = "unconfigured"
        revision_changed = cursor["schedule_revision"] != state["revision"]
        self._settle(connection, now, phase, revision_changed, thread_id=thread_id)
        discovered = self._discover(connection, now, thread_id=thread_id)
        if policy and phase == "working":
            lower = now - 1
            if previous is not None and not revision_changed:
                lower = max(previous, now - ALARM_GRACE_SECONDS - 1)
            for timestamp in alarm_times(policy, state, lower, now):
                self._fallback(connection, timestamp, now, thread_id=thread_id)
        candidates = (
            self._candidates(connection, now, thread_id=thread_id) if phase == "working" else []
        )
        if self.include_revisits and self.scope is not None:
            candidates = with_due(connection, self.scope.owner_id, now, candidates)
        connection.execute(
            "UPDATE wake_event_cursor SET last_scan_at = ?, schedule_revision = ? WHERE id = 1",
            (now, state["revision"]),
        )
        return self._view(now, phase, discovered, candidates)

    @staticmethod
    def _view(now, phase, discovered, candidates):
        return {
            "automatic_wakeup": False,
            "observed_at": now,
            "phase": phase,
            "discovered_messages": discovered,
            "candidates": [candidate for candidate in candidates if candidate["ready_at"] <= now],
            "waiting": sum(candidate["ready_at"] > now for candidate in candidates),
        }

    @staticmethod
    def _settle(connection, now, phase, revision_changed, *, thread_id=None):
        scope = " AND thread_id=?" if thread_id else ""
        parameters = (thread_id,) if thread_id else ()
        connection.execute(
            "UPDATE wake_events SET state = 'done' WHERE kind = 'message' AND state = 'pending' "
            "AND EXISTS (SELECT 1 FROM message_reply_state AS choice "
            "WHERE choice.message_id = wake_events.message_id "
            "AND choice.state IN ('replied','no_reply','deferred'))" + scope,
            parameters,
        )
        connection.execute(
            "UPDATE wake_events SET state = 'expired' "
            "WHERE kind = 'fallback' AND state = 'pending' "
            "AND (scheduled_at < ? OR ? OR ?)" + scope,
            (now - ALARM_GRACE_SECONDS, phase != "working", revision_changed, *parameters),
        )
        connection.execute(
            "UPDATE wake_events SET state = 'done' WHERE kind = 'fallback' AND state = 'pending' "
            "AND NOT EXISTS (SELECT 1 FROM wake_events AS incoming "
            "WHERE incoming.thread_id = wake_events.thread_id "
            "AND incoming.kind = 'message' AND incoming.state = 'pending')" + scope,
            parameters,
        )

    @staticmethod
    def _discover(connection, now, *, thread_id=None):
        scope = " AND incoming.thread_id=? " if thread_id else " "
        parameters = [thread_id, SCAN_LIMIT] if thread_id else [SCAN_LIMIT]
        rows = connection.execute(
            "SELECT incoming.message_id AS id, incoming.thread_id "
            "FROM message_reply_state AS incoming "
            "WHERE incoming.state IN ('unprocessed','unknown') "
            "AND NOT EXISTS (SELECT 1 FROM wake_events WHERE message_id = incoming.message_id)"
            + scope
            + "ORDER BY incoming.sequence, incoming.created_at, incoming.source_rowid LIMIT ?",
            parameters,
        ).fetchall()
        connection.executemany(
            "INSERT INTO wake_events (id, event_key, kind, thread_id, message_id, observed_at) "
            "VALUES (?, ?, 'message', ?, ?, ?)",
            [
                (str(uuid4()), "message:" + row["id"], row["thread_id"], row["id"], now)
                for row in rows
            ],
        )
        return len(rows)

    @staticmethod
    def _fallback(connection, timestamp, now, *, thread_id=None):
        scope = " AND wake_events.thread_id=?" if thread_id else ""
        parameters = [now, thread_id] if thread_id else [now]
        rows = connection.execute(
            "SELECT DISTINCT wake_events.thread_id FROM wake_events "
            + DELIVERY_JOIN
            + "WHERE "
            + ELIGIBLE
            + scope,
            parameters,
        ).fetchall()
        connection.executemany(
            "INSERT INTO wake_events "
            "(id, event_key, kind, thread_id, scheduled_at, observed_at) "
            "VALUES (?, ?, 'fallback', ?, ?, ?) ON CONFLICT(event_key) DO NOTHING",
            [
                (
                    str(uuid4()),
                    f"fallback:{timestamp}:{row['thread_id']}",
                    row["thread_id"],
                    timestamp,
                    now,
                )
                for row in rows
            ],
        )

    @staticmethod
    def _candidates(connection, now, *, thread_id=None):
        scope = " AND wake_events.thread_id=?" if thread_id else ""
        parameters = [now, thread_id] if thread_id else [now]
        threads = connection.execute(
            "SELECT wake_events.thread_id, min(observed_at) AS first_seen FROM wake_events "
            + DELIVERY_JOIN
            + "WHERE "
            + ELIGIBLE
            + scope
            + " GROUP BY wake_events.thread_id ORDER BY first_seen, wake_events.thread_id",
            parameters,
        ).fetchall()
        candidates = []
        for thread in threads:
            owner = connection.execute(
                "SELECT owner_id FROM threads WHERE id=?", (thread["thread_id"],)
            ).fetchone()[0]
            if blocking_reason(connection, owner, "reply", now):
                continue
            selection_sql = (
                "SELECT wake_events.id AS event_id, messages.id AS message_id, observed_at, "
                "messages.conversation_id, coalesce(delivery.retry_after, 0) AS retry_after "
                "FROM wake_events JOIN messages ON wake_events.message_id = messages.id "
                + DELIVERY_JOIN
                + "WHERE "
                + ELIGIBLE
                + " AND wake_events.thread_id = ? "
            )
            first = connection.execute(
                selection_sql + "ORDER BY messages.sequence LIMIT 1",
                (now, thread["thread_id"]),
            ).fetchone()
            conversation_id = first["conversation_id"]
            messages = connection.execute(
                selection_sql
                + "AND messages.conversation_id = ? ORDER BY messages.sequence LIMIT 10",
                (now, thread["thread_id"], conversation_id),
            ).fetchall()
            fallback = connection.execute(
                "SELECT id, scheduled_at FROM wake_events "
                "WHERE thread_id = ? AND kind = 'fallback' AND state = 'pending' "
                "ORDER BY scheduled_at, id",
                (thread["thread_id"],),
            ).fetchall()
            first_seen = min(message["observed_at"] for message in messages)
            last_seen = max(message["observed_at"] for message in messages)
            ready_at = min(first_seen + MAX_WAIT_SECONDS, last_seen + DEBOUNCE_SECONDS)
            if fallback:
                ready_at = min(ready_at, fallback[0]["scheduled_at"])
            ready_at = max(ready_at, max(message["retry_after"] for message in messages))
            knock = pending_knock(connection, conversation_id, now)
            candidates.append(
                {
                    "thread_id": thread["thread_id"],
                    "conversation_id": conversation_id,
                    "message_ids": [message["message_id"] for message in messages],
                    "event_ids": [message["event_id"] for message in messages]
                    + [event["id"] for event in fallback],
                    "ready_at": ready_at,
                    "cause": "fallback" if fallback else "message",
                    "knock_received_at": knock["received_at"] if knock else None,
                }
            )
        return candidates

import secrets
import time
from dataclasses import dataclass, field

from .auth import digest
from .automatic_budget import AutomaticPolicy, budget_view, launch_budget_allows
from .automatic_guard import guard_automatic
from .automatic_queue import automatic_choice
from .database import Database
from .errors import ApiError
from .execution_control import execution_busy
from .management_lease import current_boot
from .revisit_queue import candidate_key, settle
from .run_queue import queue_run
from .wakeup_events import EventPlanner

START_LEASE_SECONDS = 30


@dataclass(frozen=True)
class DispatchTicket:
    run_id: str
    token: str = field(repr=False)
    lease_until: int


class DispatchStore:
    def __init__(
        self,
        database: Database,
        clock=time.time,
        *,
        policy: AutomaticPolicy | None = None,
        scope=None,
        include_revisits=False,
    ):
        self.database = database
        self.clock = clock
        self.policy = policy
        self.scope = scope
        self.include_revisits = include_revisits

    def reserve(self, timeout: int = 3600) -> DispatchTicket | None:
        if not 1 <= timeout <= 3600:
            raise ValueError("Run timeout must be 1–3600 seconds")
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            plan = EventPlanner(
                self.database, self.clock, scope=self.scope, include_revisits=self.include_revisits
            ).plan_in_transaction(connection, now)
            if not plan["candidates"]:
                return None
            if execution_busy(connection):
                return None
            if self.policy is not None:
                budget = budget_view(connection, self.policy, now, timeout)
                if budget["blocking_reason"]:
                    raise ApiError(409, budget["blocking_reason"], "自动读信额度已用完或时钟异常。")
                timeout = budget["next_timeout"]
            if self.scope:
                choice = automatic_choice(
                    connection,
                    "reply",
                    self.scope.owner_id,
                    now,
                    current_boot(),
                    reply_candidates=plan["candidates"],
                )
                if choice is None or choice["kind"] != "reply":
                    return None
                candidate = next(
                    row for row in plan["candidates"] if candidate_key(row) == choice["key"]
                )
                self.scope.require_thread(connection, candidate["thread_id"])
            else:
                candidate = plan["candidates"][0]
            run_id = queue_run(
                connection,
                candidate["thread_id"],
                timeout,
                now,
                candidate["message_ids"],
                require_policy=self.scope is not None,
                revisit=candidate.get("revisit"),
            )
            if self.scope:
                connection.execute(
                    "INSERT INTO reply_run_scopes VALUES (?,?)", (run_id, self.scope.owner_id)
                )
            token = secrets.token_urlsafe(32)
            lease_until = min(now + START_LEASE_SECONDS, now + timeout)
            connection.execute(
                "INSERT INTO wake_dispatches (run_id, token_hash, lease_until) VALUES (?, ?, ?)",
                (run_id, digest(token), lease_until),
            )
            for event_id in candidate["event_ids"]:
                connection.execute(
                    "INSERT INTO wake_event_delivery (event_id, attempts, run_id) VALUES (?, 1, ?) "
                    "ON CONFLICT(event_id) DO UPDATE SET attempts = attempts + 1 - "
                    "EXISTS(SELECT 1 FROM reception_returned_attempts "
                    "WHERE event_id=excluded.event_id), "
                    "run_id = excluded.run_id, retry_after = 0",
                    (event_id, run_id),
                )
                connection.execute(
                    "DELETE FROM reception_returned_attempts WHERE event_id=?", (event_id,)
                )
                connection.execute("INSERT INTO wake_run_events VALUES (?, ?)", (run_id, event_id))
                connection.execute(
                    "UPDATE wake_events SET state = 'done' WHERE id = ? AND kind = 'fallback'",
                    (event_id,),
                )
            return DispatchTicket(run_id, token, lease_until)

    def authorize_launch(self, ticket: DispatchTicket) -> bool:
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            dispatch = connection.execute(
                "SELECT * FROM wake_dispatches WHERE run_id = ?", (ticket.run_id,)
            ).fetchone()
            if (
                not dispatch
                or dispatch["launched_at"] is not None
                or not secrets.compare_digest(dispatch["token_hash"], digest(ticket.token))
            ):
                return False
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (ticket.run_id,)).fetchone()
            if run["status"] != "queued" or not guard_automatic(connection, run, now):
                return False
            if self.policy is not None and not launch_budget_allows(connection, self.policy, now):
                connection.execute(
                    "UPDATE runs SET status = 'stopping', stop_reason = 'cancelled' WHERE id = ?",
                    (ticket.run_id,),
                )
                connection.execute(
                    "UPDATE wake_dispatches SET abort_reason = 'budget_closed' WHERE run_id = ?",
                    (ticket.run_id,),
                )
                return False
            connection.execute(
                "UPDATE wake_dispatches SET launched_at = ? WHERE run_id = ?",
                (now, ticket.run_id),
            )
            return True

    def is_automatic(self, run_id: str) -> bool:
        with self.database.connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM wake_dispatches WHERE run_id = ?", (run_id,)
                ).fetchone()
                is not None
            )

    def reconcile(self) -> dict:
        """Settle only confirmed terminal runs; active/unknown processes keep their occupancy."""
        from .dispatch_results import reconcile

        with self.database.connect(write=True) as connection:
            now = int(self.clock())
            summary = reconcile(connection, now)
            settle(connection, now)
            return summary

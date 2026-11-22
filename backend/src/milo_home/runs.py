import time

from .automatic_guard import guard_automatic
from .database import Database
from .errors import ApiError
from .execution_pulse import claim_pulse, heartbeat
from .knock_delivery import frozen_knock
from .legacy_permits import authorize_legacy_launch, guard_legacy_budget
from .reply_choice_result import ReplyChoiceResult
from .reply_publication import publish_rows
from .reply_result import ReplyResult
from .revisit_claims import context, read_claim
from .run_queue import queue_run

ACTIVE = ("queued", "running", "stopping")


class RunStore:
    def __init__(self, database: Database, clock=time.time, *, stop_requested=None, progress=None):
        self.database = database
        self.clock = clock
        self.stop_requested = stop_requested
        self.progress = progress

    def _progress(self):
        # Management callbacks may use SQLite, so call only outside a transaction.
        if self.progress is not None:
            self.progress()

    def _observe_stop(self, connection, run) -> bool:
        if self.stop_requested is None or not self.stop_requested():
            return False
        connection.execute(
            "UPDATE runs SET status = 'stopping', stop_reason = coalesce(stop_reason, 'cancelled') "
            "WHERE id = ? AND status IN ('queued', 'running', 'stopping')",
            (run["id"],),
        )
        return True

    def active(self) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE status IN ('queued', 'running', 'stopping')"
            ).fetchone()
        return dict(row) if row else None

    def get(self, run_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise ApiError(404, "run_not_found", "没有找到这次运行。")
        return dict(row)

    def list_owned(self, owner: str, thread_id: str) -> list[dict]:
        with self.database.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM threads WHERE id = ? AND owner_id = ?", (thread_id, owner)
            ).fetchone():
                raise ApiError(404, "thread_not_found", "没有找到这段对话。")
            rows = connection.execute(
                "SELECT * FROM runs WHERE thread_id = ? ORDER BY rowid DESC LIMIT 10",
                (thread_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def queue(self, thread_id: str, timeout: int) -> str:
        with self.database.connect(write=True) as connection:
            return queue_run(connection, thread_id, timeout, int(self.clock()))

    def authorize_launch(self, run_id: str) -> bool:
        with self.database.connect(write=True) as connection:
            now = int(self.clock())
            run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run or not guard_automatic(connection, run, now, require_launch=True):
                return False
            return authorize_legacy_launch(connection, run_id, now)

    def snapshot(self, run_id: str, *, include_history=True) -> dict:
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not run:
                raise ValueError("Missing run snapshot")
            selected = connection.execute(
                "SELECT messages.id, messages.role, messages.body, messages.sequence, "
                "messages.created_at, messages.in_reply_to "
                "FROM messages JOIN run_messages ON messages.id = run_messages.message_id "
                "WHERE run_messages.run_id = ? AND messages.thread_id = ? "
                "AND messages.conversation_id = ? "
                "ORDER BY messages.sequence",
                (run_id, run["thread_id"], run["conversation_id"]),
            ).fetchall()
            revisit = read_claim(connection, run_id)
            if not selected and revisit is None:
                raise ValueError("Missing run snapshot")
            correspondent = connection.execute(
                "SELECT users.display_name FROM users "
                "JOIN threads ON threads.owner_id = users.id WHERE threads.id = ?",
                (run["thread_id"],),
            ).fetchone()
            conversation = connection.execute(
                "SELECT kind, title FROM conversations WHERE id = ?", (run["conversation_id"],)
            ).fetchone()
            history = []
            if include_history and selected:
                history = connection.execute(
                    "SELECT id, role, body, sequence, created_at, in_reply_to "
                    "FROM messages WHERE thread_id = ? AND conversation_id = ? "
                    "AND sequence < ? ORDER BY sequence DESC LIMIT 7",
                    (run["thread_id"], run["conversation_id"], selected[0]["sequence"]),
                ).fetchall()
            knock = frozen_knock(connection, run_id)
        snapshot = {
            "run_id": run_id,
            "created_at": run["created_at"],
            "deadline_at": run["deadline_at"],
            "conversation_id": run["conversation_id"],
            "conversation": dict(conversation),
            "knock": knock,
            "correspondent": {
                "display_name": correspondent["display_name"],
                "source": "thread_owner_self_reported_display_name",
            },
            "selected": [dict(row) for row in selected],
        }
        if revisit is not None:
            snapshot["revisit"] = context(revisit)
        if include_history:
            snapshot["history_window"] = {
                "source": "same_conversation_before_first_selected",
                "limit": 6,
                "earlier_omitted": len(history) > 6,
            }
            snapshot["history"] = [dict(row) for row in reversed(history[:6])]
        return snapshot

    def started(self, run_id: str):
        self._progress()
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if (
                not run
                or self._observe_stop(connection, run)
                or not guard_legacy_budget(connection, run, now, require_launch=True)
                or not guard_automatic(connection, run, now, require_launch=True)
            ):
                return
            changed = connection.execute(
                "UPDATE runs SET status = 'running', started_at = ? "
                "WHERE id = ? AND status = 'queued' AND stop_reason IS NULL",
                (now, run_id),
            )
            if changed.rowcount:
                return claim_pulse(connection, run_id, now)

    def heartbeat(self, run_id: str, identity: str) -> bool:
        return heartbeat(self.database, run_id, identity, int(self.clock()))

    def checkpoint(self, run_id: str) -> dict:
        self._progress()
        with self.database.connect(write=True) as connection:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not run:
                raise ApiError(404, "run_not_found", "没有找到这次运行。")
            if self._observe_stop(connection, run):
                run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            guard_legacy_budget(connection, run, int(self.clock()))
            guard_automatic(connection, run, int(self.clock()))
            return dict(connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

    def stop(self, run_id: str, reason: str = "cancelled"):
        if reason not in {
            "cancelled",
            "timed_out",
            "host_cpu_limit",
            "output_limit",
            "interrupted",
        }:
            raise ValueError("Unknown stop reason")
        with self.database.connect(write=True) as connection:
            connection.execute(
                "UPDATE runs SET status = 'stopping', stop_reason = coalesce(stop_reason, ?) "
                "WHERE id = ? AND status IN ('queued', 'running', 'stopping')",
                (reason, run_id),
            )

    def finish_failed(self, run_id: str, code: str):
        with self.database.connect(write=True) as connection:
            row = connection.execute(
                "SELECT runs.stop_reason, wake_dispatches.abort_reason FROM runs "
                "LEFT JOIN wake_dispatches ON wake_dispatches.run_id = runs.id WHERE runs.id = ?",
                (run_id,),
            ).fetchone()
            if not row:
                raise ValueError("Missing run")
            terminal_states = {
                "cancelled": "cancelled",
                "timed_out": "timed_out",
            }
            status = terminal_states.get(row["stop_reason"], "failed")
            failure = row["stop_reason"] or code
            if row["stop_reason"] == "interrupted" and row["abort_reason"] is None:
                failure = code
            connection.execute(
                "UPDATE runs SET status = ?, ended_at = ?, failure_code = ? "
                "WHERE id = ? AND status IN ('queued', 'running', 'stopping')",
                (
                    status,
                    int(self.clock()),
                    failure,
                    run_id,
                ),
            )

    def publish(self, run_id: str, result: ReplyResult | ReplyChoiceResult) -> bool:
        self._progress()
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not run or run["status"] != "running" or run["stop_reason"]:
                return False
            if self._observe_stop(connection, run):
                return False
            if not guard_legacy_budget(connection, run, now, require_launch=True):
                return False
            if not guard_automatic(connection, run, now, require_launch=True):
                return False
            if now >= run["deadline_at"]:
                connection.execute(
                    "UPDATE runs SET status = 'stopping', stop_reason = 'timed_out' WHERE id = ?",
                    (run_id,),
                )
                return False
            publish_rows(connection, run, result, now)
        return True

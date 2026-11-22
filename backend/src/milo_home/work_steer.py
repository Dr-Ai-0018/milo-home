"""Durable supplements scoped to a single, already launched work turn."""

from .codex_rpc import ProtocolError, RequestRejected
from .execution_permits import active_window


def accepting_turn(connection, task_id, *, now=None):
    row = connection.execute(
        "SELECT p.id,w.id AS window_id,w.deadline_at FROM work_steer_turns s "
        "JOIN execution_permits p ON p.id=s.permit_id "
        "JOIN execution_windows w ON w.id=p.window_id "
        "JOIN work_tasks t ON t.id=w.task_id "
        "WHERE s.task_id=? AND s.accepting=1 AND p.state='launched' AND w.state='running' "
        "AND t.status='running' AND t.stop_requested_at IS NULL",
        (task_id,),
    ).fetchone()
    if row is None or (now is not None and now >= row["deadline_at"]):
        return None
    return row


class SameTurnSupplements:
    def __init__(self, runtime, owner, intent, lease, conversation, *, enabled):
        self.runtime = runtime
        self.owner = owner
        self.intent = intent
        self.lease = lease
        self.conversation = conversation
        self.enabled = enabled
        self.target = None
        self.claimed = None

    def open(self, connection, permit_id):
        if not self.enabled or not self.conversation.accepts_supplements:
            return
        active_window(connection, self.owner, self.intent.window_id, int(self.runtime.clock()))
        connection.execute(
            "INSERT INTO work_steer_turns VALUES (?,?,1)", (permit_id, self.intent.task_id)
        )
        self.target = permit_id

    def _claim(self):
        self.runtime._identity(self.lease)
        # Avoid taking the shared write lock on every idle protocol pump.
        with self.runtime.database.connect() as connection:
            row = self._pending(connection)
        if row is None:
            return None
        self.runtime.journal.require_turn_capacity(self.intent.stream_id)
        with self.runtime.database.connect(write=True) as connection:
            now = int(self.runtime.clock())
            active_window(connection, self.owner, self.intent.window_id, now)
            active = accepting_turn(connection, self.intent.task_id, now=now)
            if active is None or active["id"] != self.target:
                return None
            changed = connection.execute(
                "UPDATE work_inputs SET delivery='sending' WHERE id=? AND delivery='queued'",
                (row["id"],),
            ).rowcount
        self.lease.verify()
        return row if changed == 1 else None

    def _pending(self, connection):
        return connection.execute(
            "SELECT i.id,i.body FROM work_steer_inputs s JOIN work_inputs i ON i.id=s.input_id "
            "WHERE s.permit_id=? AND i.delivery='queued' ORDER BY i.sequence LIMIT 1",
            (self.target,),
        ).fetchone()

    def send_next(self):
        if self.target is None:
            return
        if not self.conversation.accepts_supplements:
            self.close()
            return
        claimed = self._claim()
        if claimed is None:
            return
        turn = self.conversation.turn_id
        self.claimed = claimed
        try:
            self.conversation.steer(claimed["body"], input_id=claimed["id"], expected_turn=turn)
        except RequestRejected:
            state = "conflict"
            # An advertised method may still be unavailable. Close intake for
            # this turn on rejection; don't send any more pending supplements.
            self.enabled = False
        else:
            state = "acknowledged"
        finally:
            self.claimed = None
        self.runtime._identity(self.lease)
        with self.runtime.database.connect(write=True) as connection:
            if (
                connection.execute(
                    "UPDATE work_inputs SET delivery=? WHERE id=? AND delivery='sending'",
                    (state, claimed["id"]),
                ).rowcount
                != 1
            ):
                raise ProtocolError("Supplement acknowledgement needs reconciliation")
            if not self.enabled:
                self._close(connection)
        self.lease.verify()

    def record_intent(self, event):
        params = event["request"]["params"]
        if self.claimed is None or params != {
            "threadId": self.conversation.binding.thread_id,
            "expectedTurnId": self.conversation.turn_id,
            "clientUserMessageId": self.claimed["id"],
            "input": [{"type": "text", "text": self.claimed["body"]}],
        }:
            raise ProtocolError("Supplement does not match its live turn and saved input")
        return event | {
            "work_steer": {
                "input_id": self.claimed["id"],
                "permit_id": self.target,
                "window_id": self.intent.window_id,
                "task_id": self.intent.task_id,
                "stream_id": self.intent.stream_id,
            }
        }

    def close(self):
        if self.target is not None:
            self.runtime._identity(self.lease)
            with self.runtime.database.connect(write=True) as connection:
                self._close(connection)
            self.lease.verify()

    def _close(self, connection):
        connection.execute(
            "UPDATE work_steer_turns SET accepting=0 WHERE permit_id=? AND accepting=1",
            (self.target,),
        )
        self.target = None

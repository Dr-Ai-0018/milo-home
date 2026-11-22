import time

from .database import Database
from .schedule_models import SchedulePolicy
from .schedule_policy import describe


def schedule_snapshot(connection):
    row = connection.execute("SELECT * FROM schedule_state WHERE id = 1").fetchone()
    if not row:
        raise ValueError("Schedule state missing")
    state = dict(row)
    policy = SchedulePolicy.model_validate_json(state["policy"]) if state["policy"] else None
    return policy, state


class ScheduleStore:
    def __init__(self, database: Database, clock=time.time):
        self.database = database
        self.clock = clock

    def snapshot(self):
        with self.database.connect() as connection:
            return schedule_snapshot(connection)

    def view(self):
        policy, state = self.snapshot()
        return describe(policy, state, int(self.clock()))

    def configure(self, policy: SchedulePolicy):
        with self.database.connect(write=True) as connection:
            connection.execute(
                "UPDATE schedule_state SET policy = ?, revision = revision + 1, updated_at = ? "
                "WHERE id = 1",
                (policy.model_dump_json(), int(self.clock())),
            )

    def pause(self, paused: bool):
        with self.database.connect(write=True) as connection:
            connection.execute(
                "UPDATE schedule_state SET paused = ?, revision = revision + 1, updated_at = ? "
                "WHERE id = 1",
                (int(paused), int(self.clock())),
            )
            if paused:
                connection.execute(
                    "UPDATE wake_dispatches "
                    "SET abort_reason = coalesce(abort_reason, 'schedule_closed') "
                    "WHERE run_id IN (SELECT id FROM runs "
                    "WHERE status IN ('queued', 'running', 'stopping') AND stop_reason IS NULL)"
                )
                connection.execute(
                    "UPDATE runs SET status = 'stopping', "
                    "stop_reason = coalesce(stop_reason, 'cancelled') "
                    "WHERE status IN ('queued', 'running', 'stopping')"
                )
                connection.execute(
                    "UPDATE execution_windows SET state=CASE WHEN state='unknown' THEN state "
                    "ELSE 'stopping' END,stop_reason=coalesce(stop_reason,'schedule_closed') "
                    "WHERE state!='closed'"
                )
                connection.execute(
                    "UPDATE work_tasks SET status='stopping',"
                    "stop_requested_at=coalesce(stop_requested_at,?),updated_at=? "
                    "WHERE id IN (SELECT task_id FROM execution_windows WHERE state='stopping')",
                    (int(self.clock()), int(self.clock())),
                )

    def override(self, phase: str | None, until: int | None):
        now = int(self.clock())
        if phase is not None:
            if phase not in {"working", "winding_down", "resting"} or until is None:
                raise ValueError("Invalid temporary phase")
            if not now < until <= now + 86400:
                raise ValueError("Temporary phase must expire within 24 hours")
        elif until is not None:
            raise ValueError("Cleared override cannot have a deadline")
        with self.database.connect(write=True) as connection:
            if (
                phase is not None
                and not connection.execute(
                    "SELECT 1 FROM schedule_state WHERE id = 1 AND policy IS NOT NULL"
                ).fetchone()
            ):
                raise ValueError("Configure a schedule before setting a temporary phase")
            connection.execute(
                "UPDATE schedule_state SET override_phase = ?, override_until = ?, "
                "revision = revision + 1, updated_at = ? WHERE id = 1",
                (phase, until, now),
            )

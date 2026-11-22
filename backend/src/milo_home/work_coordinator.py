"""Finite serial work on one resumed main; never launches or stops a process."""

from dataclasses import dataclass

from .codex_rpc import ProtocolError


@dataclass(frozen=True)
class WorkResult:
    outcome: str
    turns: int


def _next_input(runtime, owner, intent, lease):
    runtime.checkpoint(owner, intent, lease=lease)
    next_id = None
    with runtime.database.connect() as connection:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT i.id,i.delivery FROM work_inputs i "
            "WHERE i.task_id=? AND NOT EXISTS "
            "(SELECT 1 FROM work_steer_inputs s WHERE s.input_id=i.id) ORDER BY i.sequence",
            (intent.task_id,),
        ).fetchall()
        if not rows or len(rows) > 100:
            raise ProtocolError("Work input collection needs reconciliation")
        for row in rows:
            if row["delivery"] == "acknowledged":
                continue
            if row["delivery"] != "queued":
                raise ProtocolError("Earlier work input needs reconciliation")
            next_id = row["id"]
            break
    lease.verify()
    return next_id


def run_inputs(runtime, owner, intent, *, lease, delivery) -> WorkResult:
    turns = 0
    # Only queued inputs can start turns. Re-read to include a queued append
    # that won the transaction race immediately before the first claim.
    for _ in range(100):
        input_id = _next_input(runtime, owner, intent, lease)
        if input_id is None:
            if turns == 0:
                raise ProtocolError("A fresh task must have an undelivered input")
            return WorkResult("completed", turns)
        delivery.start(input_id)
        terminal = delivery.wait()
        turns += 1
        choice = getattr(delivery, "choice", None)
        if terminal == "completed" and choice is not None and choice.decision != "accepted":
            return WorkResult("completed", turns)
        if terminal != "completed":
            runtime._identity(lease)
            with runtime.database.connect(write=True) as connection:
                connection.execute(
                    "UPDATE work_inputs SET delivery='cancelled' "
                    "WHERE task_id=? AND delivery='queued'",
                    (intent.task_id,),
                )
            lease.verify()
            return WorkResult("failed" if terminal == "failed" else "cancelled", turns)
    if _next_input(runtime, owner, intent, lease) is not None:
        raise ProtocolError("Work input limit changed during execution")
    return WorkResult("completed", turns)

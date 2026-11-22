"""Explicit root recovery for fully evidenced turns, never an execution retry."""

from .codex_rpc import ProtocolError
from .execution_budget import held_budget_allows, latest_execution_time
from .execution_control import window_row, working
from .execution_lock import execution_lock
from .work_access import require_task
from .work_receipt_repair import repair_after_stop
from .work_recovery import _read_evidence, publish_results
from .work_revisits import settle_window
from .work_stop import recorded_identity, stop_family


def _coverage(connection, window, receipts):
    permits = connection.execute(
        "SELECT * FROM execution_permits WHERE window_id=?", (window["id"],)
    ).fetchall()
    launched = {p["input_id"] for p in permits if p["launched_at"] is not None}
    known = {receipt["work"]["input_id"] for _, receipt in receipts}
    if launched != known:
        return "missing_receipts"
    if connection.execute(
        "SELECT 1 FROM work_steer_inputs s JOIN work_inputs i ON i.id=s.input_id "
        "WHERE i.task_id=? AND i.delivery IN ('sending','unknown')",
        (window["task_id"],),
    ).fetchone():
        return "supplement_delivery_unknown"
    inputs = connection.execute(
        "SELECT i.* FROM work_inputs i WHERE i.task_id=? AND NOT EXISTS "
        "(SELECT 1 FROM work_steer_inputs s WHERE s.input_id=i.id)",
        (window["task_id"],),
    ).fetchall()
    for input_ in inputs:
        allowed = (
            {"sending", "unknown", "acknowledged"}
            if input_["id"] in known
            else {
                "queued",
                "cancelled",
                "conflict",
            }
        )
        if (
            input_["delivery"] == "acknowledged"
            and connection.execute(
                "SELECT 1 FROM execution_permits p JOIN work_results r ON r.permit_id=p.id "
                "WHERE p.input_id=? AND p.window_id!=? AND p.state='finished'",
                (input_["id"], window["id"]),
            ).fetchone()
        ):
            continue
        if input_["delivery"] not in allowed:
            return "input_conflict"
    return None


def _outcome(connection, task, window, receipts, now):
    if any("native_failure" in receipt for _, receipt in receipts):
        return "failed"
    if now >= window["deadline_at"] or window["stop_reason"] == "timed_out":
        return "timed_out"
    if (
        window["stop_reason"]
        or task["stop_requested_at"] is not None
        or not working(connection, now)
        or not held_budget_allows(connection, now)
    ):
        return "cancelled"
    outcomes = [r["outcome"] for _, r in receipts]
    if "interrupted" in outcomes:
        return "cancelled"
    if (
        outcomes
        and set(outcomes) == {"completed"}
        and receipts[-1][1].get("decision", {}).get("decision")
        in {"deferred", "declined", "needs_clarification"}
    ):
        return "completed"
    inputs = connection.execute(
        "SELECT count(*) FROM work_inputs i WHERE i.task_id=? AND NOT EXISTS "
        "(SELECT 1 FROM work_steer_inputs s WHERE s.input_id=i.id) AND NOT EXISTS "
        "(SELECT 1 FROM execution_permits p WHERE p.input_id=i.id AND p.window_id!=? "
        "AND p.state='finished')",
        (window["task_id"], window["id"]),
    ).fetchone()[0]
    return (
        "completed"
        if outcomes and len(outcomes) == inputs and set(outcomes) == {"completed"}
        else "failed"
    )


def _close(connection, window, receipts, proof_sequence, now):
    task = connection.execute(
        "SELECT * FROM work_tasks WHERE id=?", (window["task_id"],)
    ).fetchone()
    outcome = _outcome(connection, task, window, receipts, now)
    publish_results(connection, window, receipts, now=now)
    stream = connection.execute(
        "SELECT stream_id FROM execution_start_intents WHERE window_id=?", (window["id"],)
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO work_reconciliations VALUES (?,?,?,?,?)",
        (window["id"], stream, proof_sequence, now, outcome),
    )
    connection.execute(
        "UPDATE execution_permits SET state=CASE WHEN state='reserved' THEN 'cancelled' "
        "ELSE 'finished' END,ended_at=? WHERE window_id=? "
        "AND state IN ('reserved','launched','unknown')",
        (now, window["id"]),
    )
    for _, receipt in receipts:
        connection.execute(
            "UPDATE work_inputs SET delivery='acknowledged' WHERE id=?",
            (receipt["work"]["input_id"],),
        )
    suspended = receipts and receipts[-1][1].get("decision", {}).get("decision") in {
        "deferred",
        "declined",
        "needs_clarification",
    }
    if outcome != "completed" or not suspended:
        connection.execute(
            "UPDATE work_inputs SET delivery='cancelled' WHERE task_id=? AND delivery='queued'",
            (window["task_id"],),
        )
    connection.execute(
        "UPDATE execution_windows SET state='closed',ended_at=? WHERE id=?", (now, window["id"])
    )
    connection.execute(
        "UPDATE work_tasks SET status=?,updated_at=? WHERE id=?", (outcome, now, window["task_id"])
    )
    publish_results(connection, window, receipts, now=now)
    settle_window(connection, window["id"], now, outcome=outcome)
    return outcome


class WorkReconciliation:
    def __init__(self, runtime):
        self.runtime = runtime

    def settle(self, owner, window_id, *, repair_receipts=False, native_rollout=None):
        if type(repair_receipts) is not bool or (
            native_rollout is not None and not repair_receipts
        ):
            raise ValueError("Explicit receipt repair flag is required")
        runtime = self.runtime
        with execution_lock(runtime.database) as lease:
            window, private, receipts = _read_evidence(runtime, owner, window_id, lease)
            with runtime.database.connect(write=True) as connection:
                task = require_task(connection, owner, window["task_id"])
                if window["state"] == "closed":
                    return {"closed": True, "outcome": task["status"], "blocked_by": []}
                connection.execute(
                    "UPDATE execution_windows SET state='unknown' WHERE id=?", (window_id,)
                )
                connection.execute(
                    "UPDATE work_tasks SET status='needs_reconciliation',"
                    "updated_at=max(updated_at,?) WHERE id=?",
                    (int(runtime.clock()), window["task_id"]),
                )
            try:
                identity = recorded_identity(private["entries"], window["unit_name"])
            except ProtocolError:
                return self._blocked("missing_process_identity")
            if not stop_family(identity):
                return self._blocked("process_stop_unconfirmed")
            runtime._identity(lease)
            now = int(runtime.clock())
            proof = {"kind": "work_stop", **identity, "checked_at": now}
            stream = private["stream"]["id"]
            if repair_receipts:
                window, private, _ = _read_evidence(runtime, owner, window_id, lease)
                sequence = repair_after_stop(
                    runtime, owner, window, private, proof, lease, native_rollout=native_rollout
                )
            else:
                sequence = runtime.journal.record(stream, proof)
            # Re-read all evidence after stopping; no historical stop record is reused.
            window, private, receipts = _read_evidence(runtime, owner, window_id, lease)
            if any(r["state"] != "acknowledged" for r in private["requests"]):
                return self._blocked("pending_requests")
            with runtime.database.connect() as connection:
                connection.execute("BEGIN")
                blocked = self._check(connection, window, receipts, now)
            if blocked:
                return self._blocked(blocked)
            runtime._identity(lease)
            runtime.journal.reconcile_stream(stream, sequence)
            runtime._identity(lease)
            settled_at = int(runtime.clock())
            if settled_at < now:
                return self._blocked("clock_rewind")
            with runtime.database.connect(write=True) as connection:
                require_task(connection, owner, window["task_id"])
                window = window_row(connection, window_id)
                blocked = self._check(connection, window, receipts, settled_at)
                if blocked:
                    return self._blocked(blocked)
                outcome = _close(connection, window, receipts, sequence, settled_at)
            runtime._identity(lease)
            return {"closed": True, "outcome": outcome, "blocked_by": []}

    @staticmethod
    def _check(connection, window, receipts, now):
        latest = latest_execution_time(connection)
        if now < window["created_at"] or (latest is not None and now < latest):
            return "clock_rewind"
        if any(receipt["observed_at"] > now for _, receipt in receipts):
            return "clock_rewind"
        return _coverage(connection, window, receipts)

    @staticmethod
    def _blocked(reason):
        return {"closed": False, "outcome": "needs_reconciliation", "blocked_by": [reason]}

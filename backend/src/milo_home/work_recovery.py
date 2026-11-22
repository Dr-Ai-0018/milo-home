"""Explicit evidence inspection and result-only publication; never runs work.

No process/RPC creation, permit settlement, occupancy release or rebinding lives
here. A known turn can be published while its task still needs reconciliation.
"""

import json
from uuid import NAMESPACE_URL, uuid5

from .codex_journal import BusinessBinding
from .codex_rpc import ProtocolError
from .execution_control import window_row
from .execution_lock import execution_lock
from .work_access import require_task
from .work_business_facts import freeze_window
from .work_decision_publication import publish_decision
from .work_receipts import validated_receipts


def _read_evidence(runtime, owner, window_id, lease):
    snapshot, root, linked = runtime._snapshot(lease)
    with runtime.database.connect() as connection:
        connection.execute("BEGIN")
        window = dict(window_row(connection, window_id))
        task = require_task(connection, owner, window["task_id"])
        identity = runtime._binding(connection, owner, task["session_id"], snapshot, root)
        start = connection.execute(
            "SELECT * FROM execution_start_intents WHERE window_id=?", (window_id,)
        ).fetchone()
        if start is None or linked != BusinessBinding(binding_id=start["binding_id"], **identity):
            raise ProtocolError("Historical work association needs reconciliation")
        stream_id = start["stream_id"]
    # Private files are read after ServiceDatabase restores the root identity.
    private = runtime.journal.work_snapshot(stream_id)
    if (
        private["work"]
        != {"stream": stream_id, "window_id": window_id, "task_id": window["task_id"]}
        or private["stream"]["unit"] != window["unit_name"]
    ):
        raise ProtocolError("Historical stream differs from the reserved work")
    receipts = validated_receipts(
        private["entries"],
        stream_id=stream_id,
        window_id=window_id,
        task_id=window["task_id"],
        thread_id=runtime.journal.binding().thread_id,
    )
    runtime._identity(lease)
    with runtime.database.connect() as connection:
        connection.execute("BEGIN")
        require_task(connection, owner, window["task_id"])
        check_receipt_permits(connection, window_id, receipts)
    runtime._identity(lease)
    return window, private, receipts


def check_receipt_permits(connection, window_id, receipts):
    for _, receipt in receipts:
        work = receipt["work"]
        permit = connection.execute(
            "SELECT * FROM execution_permits WHERE id=? AND input_id=? AND window_id=?",
            (work["permit_id"], work["input_id"], window_id),
        ).fetchone()
        if (
            permit is None
            or permit["launched_at"] is None
            or receipt["observed_at"] < permit["launched_at"]
        ):
            raise ProtocolError("Work receipt differs from its launched permit")


def _result_row(window, sequence, receipt):
    work = receipt["work"]
    return {
        "id": str(uuid5(NAMESPACE_URL, "milo-home.work-result:" + work["input_id"])),
        "task_id": window["task_id"],
        "input_id": work["input_id"],
        "permit_id": work["permit_id"],
        "window_id": window["id"],
        "stream_id": work["stream_id"],
        "receipt_sequence": sequence,
        "outcome": receipt["outcome"],
        "observed_at": receipt["observed_at"],
        "output": json.dumps(receipt["output"], ensure_ascii=False, sort_keys=True)
        if "output" in receipt
        else None,
    }


def publish_results(connection, window, receipts, *, now=None):
    """Use the caller's transaction for an atomic result/event publication."""
    for sequence, receipt in receipts:
        row = _result_row(window, sequence, receipt)
        previous = connection.execute(
            "SELECT * FROM work_results WHERE input_id=?", (row["input_id"],)
        ).fetchone()
        if previous is not None:
            if dict(previous) != row:
                raise ProtocolError("Published work result differs from its evidence")
        else:
            connection.execute(
                "INSERT INTO work_results VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(row.values())
            )
        if "decision" in receipt:
            if now is None:
                raise ValueError("Work choice publication requires an explicit observation time")
            publish_decision(connection, row, receipt["decision"], now)
    freeze_window(connection, window["id"])


class WorkRecovery:
    def __init__(self, runtime):
        self.runtime = runtime

    def replay_results(self, owner, window_id):
        """Only copy validated facts. The original work is never submitted again."""
        runtime = self.runtime
        with execution_lock(runtime.database) as lease:
            window, _, receipts = _read_evidence(runtime, owner, window_id, lease)
            with runtime.database.connect(write=True) as connection:
                require_task(connection, owner, window["task_id"])
                publish_results(connection, window, receipts, now=int(runtime.clock()))
            runtime._identity(lease)
            return len(receipts)

    def inspect(self, owner, window_id):
        """Report saved evidence, without claiming a fresh process-stop check."""
        runtime = self.runtime
        with execution_lock(runtime.database) as lease:
            window, private, receipts = _read_evidence(runtime, owner, window_id, lease)
            with runtime.database.connect() as connection:
                connection.execute("BEGIN")
                task = require_task(connection, owner, window["task_id"])
                window = window_row(connection, window_id)
                permits = connection.execute(
                    "SELECT * FROM execution_permits WHERE window_id=?", (window_id,)
                ).fetchall()
                published = connection.execute(
                    "SELECT count(*) FROM work_results WHERE window_id=?", (window_id,)
                ).fetchone()[0]
                report = {
                    "task_status": task["status"],
                    "window_state": window["state"],
                    "private_stream_state": private["stream"]["state"],
                    "known_turns": len(receipts),
                    "published_turns": published,
                    "unresolved_permits": sum(
                        p["state"] in {"launched", "unknown"} for p in permits
                    ),
                    "missing_receipts": sum(p["launched_at"] is not None for p in permits)
                    - len(receipts),
                }
            phases = [e.get("phase") for _, e in private["entries"] if e["kind"] == "process"]
            report["recorded_stop"] = next(
                (p for p in reversed(phases) if p in {"stop_confirmed", "stop_unknown"}), "missing"
            )
            report["requires_reconciliation"] = (
                report["window_state"] != "closed"
                or report["private_stream_state"] != "closed"
                or report["missing_receipts"] != 0
                or report["unresolved_permits"] != 0
            )
            report["publication_pending"] = published < len(receipts)
            runtime._identity(lease)
            return report

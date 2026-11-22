"""Display evidence only; never renew execution leases or authorize a launch."""

import hashlib
import secrets

PULSE_INTERVAL = 15
PULSE_TTL = 45


def identity_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()


def claim_pulse(connection, run_id: str, now: int) -> str:
    identity = secrets.token_urlsafe(32)
    connection.execute(
        "INSERT INTO run_activity (run_id, identity_hash, started_at, heartbeat_at) "
        "VALUES (?, ?, ?, ?)",
        (run_id, identity_hash(identity), now, now),
    )
    return identity


def heartbeat(database, run_id: str, identity: str, now: int) -> bool:
    with database.connect(write=True) as connection:
        row = connection.execute(
            "SELECT run_activity.*, runs.status, runs.stop_reason, runs.created_at, "
            "runs.started_at AS run_started_at, runs.deadline_at, runs.unit_name "
            "FROM run_activity JOIN runs ON runs.id = run_activity.run_id "
            "WHERE run_id = ? AND identity_hash = ?",
            (run_id, identity_hash(identity)),
        ).fetchone()
        if not row or row["status"] != "running" or row["stop_reason"]:
            return False
        if not pulse_consistent(row, now):
            connection.execute(
                "UPDATE run_activity SET clock_invalid = 1 WHERE run_id = ?", (run_id,)
            )
            return False
        connection.execute(
            "UPDATE run_activity SET heartbeat_at = ? WHERE run_id = ?", (now, run_id)
        )
        return True


def pulse_consistent(row, now: int) -> bool:
    return (
        not row["clock_invalid"]
        and row["unit_name"] == "milo-reply-" + row["run_id"]
        and row["started_at"] == row["run_started_at"]
        and row["created_at"] <= row["started_at"] <= row["heartbeat_at"] <= now
        and row["deadline_at"] is not None
        and now < row["deadline_at"]
    )

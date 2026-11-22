"""Immutable per-run revisit sources, separate from the original message delivery identity."""

from .errors import ApiError


def sources(connection, revisit_id):
    return connection.execute(
        "SELECT m.* FROM revisit_sources s JOIN message_reply_state m "
        "ON m.message_id=s.message_id WHERE s.revisit_id=? ORDER BY m.sequence",
        (revisit_id,),
    ).fetchall()


def claim(connection, run_id, revisit_id, revision, now):
    if not connection.in_transaction:
        raise ValueError("Revisit claim requires the execution reservation transaction")
    row = connection.execute("SELECT * FROM revisit_intents WHERE id=?", (revisit_id,)).fetchone()
    if row is None or row["revision"] != revision or row["state"] not in {"scheduled", "blocked"}:
        raise ApiError(409, "revisit_changed", "回访安排已经变化，请重新核对。")
    selected = sources(connection, revisit_id)
    if not 1 <= len(selected) <= 10 or any(item["revision"] is None for item in selected):
        raise ValueError("Revisit requires bounded, persisted original decisions")
    connection.execute(
        "INSERT INTO revisit_claims VALUES (?,?,?,?,?)",
        (run_id, revisit_id, revision, row["due_at"], now),
    )
    connection.executemany(
        "INSERT INTO revisit_claim_sources VALUES (?,?,?,?)",
        [(run_id, item["message_id"], item["revision"], item["state"]) for item in selected],
    )
    connection.execute(
        "UPDATE revisit_intents SET state='running',blocked_reason=NULL,updated_at=? WHERE id=?",
        (now, revisit_id),
    )


def read_claim(connection, run_id):
    row = connection.execute("SELECT * FROM revisit_claims WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["sources"] = [
        dict(item)
        for item in connection.execute(
            "SELECT * FROM revisit_claim_sources WHERE run_id=? ORDER BY message_id", (run_id,)
        )
    ]
    if not result["sources"]:
        raise ValueError("Revisit source evidence is missing")
    return result


def source_ids(connection, run_id):
    saved = read_claim(connection, run_id)
    return [item["message_id"] for item in saved["sources"]] if saved else []


def context(saved):
    if saved is None:
        return None
    return {
        "id": saved["revisit_id"],
        "revision": saved["revision"],
        "due_at": saved["due_at"],
        "source_ids": [row["message_id"] for row in saved["sources"]],
    }


def claim_current(connection, run_id):
    row = connection.execute(
        "SELECT v.revision,v.state,c.revision AS claimed_revision FROM revisit_claims c "
        "JOIN revisit_intents v ON v.id=c.revisit_id WHERE c.run_id=?",
        (run_id,),
    ).fetchone()
    return row is None or (row["revision"] == row["claimed_revision"] and row["state"] == "running")

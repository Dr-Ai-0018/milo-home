"""Attach one saved greeting to ordinary work; success is the consumption watermark."""


def pending_knock(connection, conversation_id: str, now: int):
    # Accepted anchors strictly advance per account under the fixed 30-second window.
    # Coalesced receipts never become additional greetings, including across topics.
    signal = connection.execute(
        "SELECT id, received_at FROM knocks WHERE conversation_id = ? "
        "AND disposition = 'accepted' AND received_at <= ? "
        "ORDER BY received_at DESC LIMIT 1",
        (conversation_id, now),
    ).fetchone()
    if signal is None:
        return None
    consumed = connection.execute(
        "SELECT 1 FROM run_knocks JOIN runs ON runs.id = run_knocks.run_id "
        "JOIN knocks ON knocks.id = run_knocks.knock_id "
        "WHERE runs.status = 'succeeded' AND runs.conversation_id = ? "
        "AND knocks.received_at >= ? LIMIT 1",
        (conversation_id, signal["received_at"]),
    ).fetchone()
    return None if consumed else signal


def freeze_knock(connection, run_id: str, conversation_id: str, now: int):
    if not connection.in_transaction:
        raise ValueError("Knock attachment requires an existing transaction")
    signal = pending_knock(connection, conversation_id, now)
    if signal is not None:
        connection.execute("INSERT INTO run_knocks VALUES (?, ?)", (run_id, signal["id"]))


def frozen_knock(connection, run_id: str):
    signal = connection.execute(
        "SELECT knocks.received_at, knocks.disposition, knocks.conversation_id, "
        "runs.conversation_id AS run_conversation, runs.created_at FROM run_knocks "
        "JOIN runs ON runs.id = run_knocks.run_id "
        "JOIN knocks ON knocks.id = run_knocks.knock_id WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if signal is None:
        return None
    if (
        signal["disposition"] != "accepted"
        or signal["conversation_id"] != signal["run_conversation"]
        or signal["received_at"] > signal["created_at"]
    ):
        raise ValueError("Invalid run knock snapshot")
    return {
        "source": "saved_greeting_for_this_conversation",
        "received_at": signal["received_at"],
    }

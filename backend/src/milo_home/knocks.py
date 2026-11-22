"""Persist a greeting without granting permission to wake or retry execution."""

import time
from typing import Literal
from uuid import UUID, uuid4

from .conversations import ConversationService
from .errors import ApiError
from .schedule_policy import phase_at
from .schedules import schedule_snapshot
from .schemas import InputModel

VIEW_FIELDS = "id, conversation_id, disposition, received_at, next_allowed_at, response_phase"


class KnockInput(InputModel):
    pass


class KnockView(InputModel):
    id: UUID
    conversation_id: UUID
    disposition: Literal["accepted", "coalesced"]
    received_at: int
    next_allowed_at: int
    response_phase: Literal["working", "winding_down", "resting", "paused", "unknown"]


def response_phase(connection, now: int) -> str:
    try:
        policy, state = schedule_snapshot(connection)
        if state["updated_at"] > now:
            return "unknown"
        if state["paused"]:
            return "paused"
        return phase_at(policy, state, now)[0] if policy else "unknown"
    except ValueError:
        return "unknown"


class KnockService:
    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock

    def receive(self, owner: str, conversation_id: str, key: str):
        with self.database.connect(write=True) as connection:
            conversation = ConversationService._require_owner(connection, owner, conversation_id)
            previous = connection.execute(
                f"SELECT {VIEW_FIELDS} FROM knocks WHERE owner_id = ? AND client_key = ?",
                (owner, key),
            ).fetchone()
            if previous:
                if previous["conversation_id"] != conversation_id:
                    raise ApiError(409, "idempotency_conflict", "请求标识已用于另一个敲门入口。")
                return dict(previous), False
            now = int(self.clock())
            signal = connection.execute(
                "SELECT id, next_allowed_at FROM knocks WHERE owner_id = ? "
                "AND disposition = 'accepted' ORDER BY received_at DESC LIMIT 1",
                (owner,),
            ).fetchone()
            receipt_id = str(uuid4())
            coalesced = signal is not None and now < signal["next_allowed_at"]
            connection.execute(
                "INSERT INTO knocks (id, owner_id, thread_id, conversation_id, client_key, "
                "signal_id, disposition, received_at, next_allowed_at, response_phase) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_id,
                    owner,
                    conversation["thread_id"],
                    conversation_id,
                    key,
                    signal["id"] if coalesced else receipt_id,
                    "coalesced" if coalesced else "accepted",
                    now,
                    signal["next_allowed_at"] if coalesced else now + 30,
                    response_phase(connection, now),
                ),
            )
            return dict(
                connection.execute(
                    f"SELECT {VIEW_FIELDS} FROM knocks WHERE id = ?", (receipt_id,)
                ).fetchone()
            ), True

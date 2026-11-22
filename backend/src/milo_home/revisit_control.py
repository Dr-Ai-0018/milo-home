"""Explicit revision-controlled revisit changes, separate from execution permissions."""

import time
from uuid import uuid4

from .errors import ApiError


def require_intent(connection, owner, identity):
    row = connection.execute(
        "SELECT * FROM revisit_intents WHERE id=? AND owner_id=?", (identity, owner)
    ).fetchone()
    if row is None:
        raise ApiError(404, "revisit_not_found", "没有找到本人的回访安排。")
    return row


def resolve_deferred(connection, row, control, now):
    pending = connection.execute(
        "SELECT message_id,revision FROM message_reply_state "
        "WHERE state='deferred' AND revisit_id=?",
        (row["id"],),
    ).fetchall()
    connection.executemany(
        "INSERT INTO message_control_decisions VALUES (?,?,?,'no_reply',?)",
        [(item["message_id"], item["revision"] + 1, control, now) for item in pending],
    )


class RevisitControl:
    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock

    def change(self, owner, identity, expected_revision, *, action, due_at=None, no_reply=False):
        now = int(self.clock())
        if (
            action not in {"reschedule", "cancel"}
            or type(expected_revision) is not int
            or expected_revision < 1
        ):
            raise ValueError("An explicit revisit action and expected revision are required")
        if (action == "reschedule" and (type(due_at) is not int or due_at <= now)) or (
            action == "cancel" and due_at is not None
        ):
            raise ValueError("Revisit reschedule requires one future timestamp")
        with self.database.connect(write=True) as connection:
            row = require_intent(connection, owner, identity)
            if row["revision"] != expected_revision:
                raise ApiError(409, "revisit_changed", "回访已被修改，旧修改不会覆盖新安排。")
            if row["state"] in {"completed", "cancelled"}:
                raise ApiError(409, "revisit_finished", "这份回访已经收口，不能重新唤起。")
            if now < row["updated_at"]:
                raise ValueError("Revisit control clock moved backwards")
            active = connection.execute(
                "SELECT r.* FROM revisit_claims c JOIN runs r ON r.id=c.run_id "
                "WHERE c.revisit_id=? AND r.status IN ('queued','running','stopping')",
                (identity,),
            ).fetchall()
            for run in active:
                if connection.execute(
                    "SELECT 1 FROM reply_execution_results WHERE run_id=?", (run["id"],)
                ).fetchone():
                    raise ApiError(409, "publication_pending", "原回访结果已经确认，请先核对回存。")
            if (
                action == "cancel"
                and not no_reply
                and connection.execute(
                    "SELECT 1 FROM message_reply_state WHERE revisit_id=? AND state='deferred'",
                    (identity,),
                ).fetchone()
            ):
                raise ApiError(
                    409, "revisit_disposition_required", "取消暂缓回访时，需明确这次不再接话。"
                )
            control = str(uuid4())
            revision = expected_revision + 1
            connection.execute(
                "INSERT INTO revisit_controls VALUES (?,?,?,?,?,?,?,?)",
                (control, owner, identity, expected_revision, revision, action, due_at, now),
            )
            if action == "cancel":
                resolve_deferred(connection, row, control, now)
            connection.execute(
                "UPDATE revisit_intents SET revision=?,state=?,due_at=?,updated_at=?,"
                "blocked_reason=NULL WHERE id=?",
                (
                    revision,
                    "scheduled" if action == "reschedule" else "cancelled",
                    due_at if due_at is not None else row["due_at"],
                    now,
                    identity,
                ),
            )
            for run in active:
                connection.execute(
                    "UPDATE runs SET status='stopping',"
                    "stop_reason=coalesce(stop_reason,'cancelled') "
                    "WHERE id=?",
                    (run["id"],),
                )
                connection.execute(
                    "UPDATE wake_dispatches SET "
                    "abort_reason=coalesce(abort_reason,'revisit_changed') "
                    "WHERE run_id=?",
                    (run["id"],),
                )
            return dict(require_intent(connection, owner, identity))

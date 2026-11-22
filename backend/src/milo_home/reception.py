"""Owner reception history and two effective slots, independent of execution occupancy."""

from .errors import ApiError
from .interaction_timing import ReceptionRequest, ReceptionView

KINDS = ("reply", "work")


def targets(scope):
    if scope not in (*KINDS, "all"):
        raise ValueError("Unknown reception scope")
    return KINDS if scope == "all" else (scope,)


def current(connection, owner, kind):
    if kind not in KINDS:
        raise ValueError("Reception observation requires one input category")
    return connection.execute(
        "SELECT c.* FROM reception_slots s JOIN reception_changes c "
        "ON c.owner_id=s.owner_id AND c.revision=s.revision WHERE s.owner_id=? AND s.kind=?",
        (owner, kind),
    ).fetchone()


def blocking_reason(connection, owner, kind, now):
    row = current(connection, owner, kind)
    if row is None:
        return None
    if now < row["recorded_at"]:
        return "reception_clock_invalid"
    if row["mode"] == "available" or (row["until"] is not None and row["until"] <= now):
        return None
    return "reception_closed"


def require_reception(connection, owner, kind, now):
    reason = blocking_reason(connection, owner, kind, now)
    if reason:
        raise ApiError(409, reason, "接收安排尚未开放，输入已保存，稍后再看。")


def observe(connection, owner, kind, now, valid_until):
    row = current(connection, owner, kind)
    if row is None or row["recorded_at"] > now:
        return None
    until = min(valid_until, row["until"]) if row["until"] is not None else valid_until
    if until <= now:
        # A read never rewrites the arrangement or replays its expired observation.
        # The next write-side tick records the automatic recovery revision.
        return None
    return ReceptionView(
        **{
            key: row[key]
            for key in ("scope", "mode", "resume_on", "until", "revision", "updated_at")
        },
        observed_at=now,
        valid_until=until,
    )


def save(
    connection, owner, request, now, *, decided_at=None, cause="control", run_id=None, apply=True
):
    if not connection.in_transaction:
        raise ValueError("Reception updates require an existing transaction")
    request = ReceptionRequest.model_validate(request)
    decided_at = now if decided_at is None else decided_at
    if decided_at > now or (request.until is not None and request.until <= decided_at):
        raise ValueError("Reception update requires consistent original decision times")
    latest = connection.execute(
        "SELECT coalesce(max(recorded_at),0),coalesce(max(revision),0) "
        "FROM reception_changes WHERE owner_id=?",
        (owner,),
    ).fetchone()
    if latest[0] > now:
        raise ValueError("Reception clock moved backwards")
    revision = latest[1] + 1
    connection.execute(
        "INSERT INTO reception_changes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            owner,
            revision,
            request.scope,
            request.mode,
            request.resume_on,
            request.until,
            decided_at,
            now,
            cause,
            run_id,
            int(apply),
        ),
    )
    if apply:
        connection.executemany(
            "INSERT INTO reception_slots VALUES (?,?,?) ON CONFLICT(owner_id,kind) "
            "DO UPDATE SET revision=excluded.revision",
            [(owner, kind, revision) for kind in targets(request.scope)],
        )
    return revision


def recover(connection, owner, kind, now, *, cause):
    return save(
        connection,
        owner,
        ReceptionRequest(scope=kind, mode="available", resume_on=None, until=None),
        now,
        cause=cause,
    )


def settle_due(connection, now, *, owner=None):
    scope = " AND s.owner_id=?" if owner is not None else ""
    rows = connection.execute(
        "SELECT s.owner_id,s.kind FROM reception_slots s JOIN reception_changes c "
        "ON c.owner_id=s.owner_id AND c.revision=s.revision "
        "WHERE c.until<=? AND c.recorded_at<=?" + scope,
        (now, now, owner) if owner is not None else (now, now),
    ).fetchall()
    for row in rows:
        recover(connection, row["owner_id"], row["kind"], now, cause="at_time")


def on_new_input(connection, owner, kind, now):
    """Called only after an authenticated, non-deduplicated insert in this transaction."""
    settle_due(connection, now, owner=owner)
    row = current(connection, owner, kind)
    if row and row["resume_on"] == "new_input" and row["recorded_at"] <= now:
        recover(connection, owner, kind, now, cause="new_input")


def input_cursor(connection, owner, kind):
    table, clause = (
        ("messages", "author_id=? AND role='visitor'")
        if kind == "reply"
        else (
            "work_inputs",
            "owner_id=? AND NOT EXISTS "
            "(SELECT 1 FROM work_revisit_inputs s WHERE s.input_id=work_inputs.id)",
        )
    )
    return connection.execute(
        f"SELECT coalesce(max(rowid),0) FROM {table} WHERE {clause}", (owner,)
    ).fetchone()[0]


def freeze_run(connection, run_id, owner):
    revisions = [current(connection, owner, kind) for kind in KINDS]
    connection.execute(
        "INSERT INTO reception_run_bases VALUES (?,?,?,?,?)",
        (
            run_id,
            *[row["revision"] if row else 0 for row in revisions],
            *[input_cursor(connection, owner, kind) for kind in KINDS],
        ),
    )

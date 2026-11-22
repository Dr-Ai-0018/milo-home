"""Apply original reception requests once; subsequent choices never rewrite birth evidence."""

from .reception import current, input_cursor, recover, save, settle_due, targets


def publish_reception(connection, run, request, now, completed_at):
    owner = connection.execute(
        "SELECT owner_id FROM threads WHERE id=?", (run["thread_id"],)
    ).fetchone()[0]
    base = connection.execute(
        "SELECT * FROM reception_run_bases WHERE run_id=?", (run["id"],)
    ).fetchone()
    if base is None:
        raise ValueError("Reception result has no original intake baseline")
    apply = all(
        (row["revision"] if (row := current(connection, owner, kind)) else 0)
        == base[kind + "_revision"]
        for kind in targets(request.scope)
    )
    save(
        connection,
        owner,
        request,
        now,
        decided_at=completed_at,
        cause="result",
        run_id=run["id"],
        apply=apply,
    )
    if apply and request.resume_on == "new_input":
        for kind in targets(request.scope):
            if input_cursor(connection, owner, kind) > base[kind + "_input"]:
                recover(connection, owner, kind, now, cause="new_input")
    settle_due(connection, now, owner=owner)


def read_reception_projection(connection, run_id):
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM reception_changes WHERE origin_run_id=?", (run_id,)
        )
    ]


def require_reception_projection(request, run, rows, completed_at):
    if request is None:
        if rows:
            raise ValueError("Unexpected reception result projection")
        return
    if len(rows) != 1:
        raise ValueError("Reception result birth evidence is missing")
    row = rows[0]
    if (
        {key: row[key] for key in ("scope", "mode", "resume_on", "until")} != request.model_dump()
        or row["origin_run_id"] != run["id"]
        or row["cause"] != "result"
        or row["updated_at"] != completed_at
    ):
        raise ValueError("Reception birth evidence differs from the sealed request")

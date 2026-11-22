"""Apply a work result through the shared reception history with an explicit work origin."""

from .reception import current, input_cursor, recover, save, settle_due, targets


def freeze_turn(connection, permit_id, owner):
    revisions = [current(connection, owner, kind) for kind in ("reply", "work")]
    connection.execute(
        "INSERT INTO work_turn_bases VALUES (?,?,?,?,?)",
        (
            permit_id,
            *[row["revision"] if row else 0 for row in revisions],
            input_cursor(connection, owner, "reply"),
            input_cursor(connection, owner, "work"),
        ),
    )


def publish_reception(connection, result, request, now):
    owner = connection.execute(
        "SELECT owner_id FROM work_tasks WHERE id=?", (result["task_id"],)
    ).fetchone()[0]
    base = connection.execute(
        "SELECT * FROM work_turn_bases WHERE permit_id=?", (result["permit_id"],)
    ).fetchone()
    if base is None:
        raise ValueError("Work reception result has no original launch baseline")
    apply = all(
        (row["revision"] if (row := current(connection, owner, kind)) else 0)
        == base[kind + "_revision"]
        for kind in targets(request.scope)
    )
    # The legacy history's result origin is a reply-run FK. Use its controlled
    # update path and retain the actual work-result origin in the adjacent map;
    # never invent an ordinary reply run just to satisfy that older schema.
    revision = save(connection, owner, request, now, decided_at=result["observed_at"], apply=apply)
    connection.execute(
        "INSERT INTO work_result_receptions VALUES (?,?,?)", (result["id"], owner, revision)
    )
    if apply and request.resume_on == "new_input":
        for kind in targets(request.scope):
            if input_cursor(connection, owner, kind) > base[kind + "_input"]:
                recover(connection, owner, kind, now, cause="new_input")
    settle_due(connection, now, owner=owner)

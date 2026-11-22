"""Finish the claimed revisit, including an explicit quiet end for omitted old deferrals."""

from .reply_choice_result import NoReplyChoice


def choices_for_publication(result, saved):
    choices = list(result.decisions)
    covered = {str(value) for choice in choices for value in choice.message_ids}
    omitted = (
        [
            row["message_id"]
            for row in saved["sources"]
            if row["decision_state"] == "deferred" and row["message_id"] not in covered
        ]
        if saved
        else []
    )
    if omitted:
        choices.append(NoReplyChoice(kind="no_reply", message_ids=omitted))
    return choices


def complete(connection, saved, now):
    if saved is not None:
        changed = connection.execute(
            "UPDATE revisit_intents SET state='completed',updated_at=?,blocked_reason=NULL "
            "WHERE id=? AND revision=? AND state='running'",
            (now, saved["revisit_id"], saved["revision"]),
        )
        if changed.rowcount != 1:
            raise ValueError("Revisit changed before original publication")

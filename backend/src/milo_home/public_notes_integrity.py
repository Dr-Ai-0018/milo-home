"""Read-only checks of the content history and publication reference graph."""

REQUIRED_TRIGGERS = {
    "public_note_revision_immutable",
    "public_note_revision_no_delete",
    "public_note_first_publication_immutable",
}


def public_notes_constraints_ok(connection):
    triggers = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    if not REQUIRED_TRIGGERS.issubset(triggers):
        return False
    invalid = connection.execute(
        "SELECT 1 FROM public_notes n "
        "LEFT JOIN public_note_revisions published "
        "ON published.note_id=n.id AND published.revision=n.published_revision "
        "WHERE n.latest_revision IS NOT "
        "(SELECT max(r.revision) FROM public_note_revisions r WHERE r.note_id=n.id) "
        "OR (n.published_revision IS NOT NULL AND published.note_id IS NULL) LIMIT 1"
    ).fetchone()
    return invalid is None

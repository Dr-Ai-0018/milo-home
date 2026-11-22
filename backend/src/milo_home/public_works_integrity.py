"""Read-only validation of work publication pointers and retained asset references."""

from .public_work_records import saved_content, selected_assets

REQUIRED_TRIGGERS = {
    "public_work_revision_immutable",
    "public_work_revision_no_delete",
    "public_work_asset_immutable",
    "public_work_asset_no_delete",
    "public_work_selection_immutable",
    "public_work_selection_no_delete",
    "public_work_selection_no_append",
    "public_work_first_publication_immutable",
    "public_work_no_delete",
}
REQUIRED_INDEXES = {"public_work_asset_references"}


def public_works_constraints_ok(connection):
    objects = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('trigger','index')"
        )
    }
    if not (REQUIRED_TRIGGERS | REQUIRED_INDEXES).issubset(objects):
        return False
    invalid = connection.execute(
        "SELECT 1 FROM public_works w LEFT JOIN public_work_revisions p "
        "ON p.work_id=w.id AND p.revision=w.published_revision "
        "WHERE w.latest_revision IS NOT "
        "(SELECT max(r.revision) FROM public_work_revisions r WHERE r.work_id=w.id) "
        "OR (w.published_revision IS NOT NULL AND p.work_id IS NULL) LIMIT 1"
    ).fetchone()
    if invalid is not None:
        return False
    try:
        for row in connection.execute("SELECT work_id,revision FROM public_work_revisions"):
            content = saved_content(connection, row["work_id"], row["revision"])
            selected_assets(connection, row["work_id"], content)
        return True
    except ValueError:
        return False

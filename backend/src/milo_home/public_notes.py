"""Local explicit publishing and anonymous reads of current public revisions."""

import time
from uuid import UUID

from .errors import ApiError
from .public_notes_models import PublicNoteInput

CONTENT_FIELDS = ("title", "summary", "body", "category", "content_date")
PUBLIC_COLUMNS = (
    "n.id,n.sequence,r.revision,r.title,r.summary,r.category,r.content_date,"
    "n.published_at,n.public_updated_at AS updated_at"
)


def content_dict(content):
    return PublicNoteInput.model_validate(content).model_dump(mode="json")


def entry(connection, note_id):
    row = connection.execute(
        "SELECT * FROM public_notes WHERE id=?", (str(UUID(note_id)),)
    ).fetchone()
    if row is None:
        raise ApiError(404, "note_not_found", "这条近况当前不可读取。")
    return row


def saved_content(connection, note_id, revision):
    if revision is None:
        return None
    row = connection.execute(
        "SELECT title,summary,body,category,content_date FROM public_note_revisions "
        "WHERE note_id=? AND revision=?",
        (note_id, revision),
    ).fetchone()
    if row is None:
        raise ValueError("Content revision is missing")
    return dict(row)


def local_view(connection, row):
    return {
        **dict(row),
        "latest": saved_content(connection, row["id"], row["latest_revision"]),
        "published": saved_content(connection, row["id"], row["published_revision"]),
    }


def append_revision(connection, note_id, revision, content, now):
    connection.execute(
        "INSERT INTO public_note_revisions "
        "(note_id,revision,title,summary,body,category,content_date,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (note_id, revision, *[content[key] for key in CONTENT_FIELDS], now),
    )


def require_version(row, expected_version):
    if row["version"] != expected_version:
        raise ApiError(409, "note_changed", "近况已发生变化，请重新核对当前版本。")


def next_sequence(connection):
    return connection.execute("SELECT coalesce(max(sequence),0)+1 FROM public_notes").fetchone()[0]


class PublicNotes:
    def __init__(self, database, *, clock=time.time):
        self.database = database
        self.clock = clock

    def show(self, note_id):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            return local_view(connection, entry(connection, note_id))

    def create(self, note_id, content, *, source_key=None):
        note_id, content = str(UUID(note_id)), content_dict(content)
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            row = connection.execute("SELECT * FROM public_notes WHERE id=?", (note_id,)).fetchone()
            if row is not None:
                if (
                    saved_content(connection, note_id, 1) != content
                    or row["source_key"] != source_key
                ):
                    raise ApiError(409, "note_exists", "这个近况身份已有不同内容。")
                return local_view(connection, row)
            connection.execute(
                "INSERT INTO public_notes "
                "(id,version,latest_revision,source_key,created_at,updated_at) "
                "VALUES (?,1,1,?,?,?)",
                (note_id, source_key, now, now),
            )
            append_revision(connection, note_id, 1, content, now)
            return local_view(connection, entry(connection, note_id))

    def revise(self, note_id, content, expected_version):
        content = content_dict(content)
        with self.database.connect(write=True) as connection:
            row = entry(connection, note_id)
            require_version(row, expected_version)
            if saved_content(connection, row["id"], row["latest_revision"]) == content:
                return local_view(connection, row)
            revision, now = row["latest_revision"] + 1, int(self.clock())
            append_revision(connection, row["id"], revision, content, now)
            connection.execute(
                "UPDATE public_notes SET latest_revision=?,version=version+1,updated_at=? "
                "WHERE id=?",
                (revision, now, row["id"]),
            )
            return local_view(connection, entry(connection, row["id"]))

    def import_published(self, note_id, content, *, source_key):
        """Import one missing, explicitly public legacy item without a half-saved draft."""
        note_id, content = str(UUID(note_id)), content_dict(content)
        with self.database.connect(write=True) as connection:
            row = connection.execute("SELECT * FROM public_notes WHERE id=?", (note_id,)).fetchone()
            if row is not None:
                return local_view(connection, row), False
            now = int(self.clock())
            connection.execute(
                "INSERT INTO public_notes "
                "(id,version,latest_revision,published_revision,sequence,source_key,"
                "created_at,updated_at,published_at,public_updated_at) "
                "VALUES (?,2,1,1,?,?,?,?,?,?)",
                (note_id, next_sequence(connection), source_key, now, now, now, now),
            )
            append_revision(connection, note_id, 1, content, now)
            return local_view(connection, entry(connection, note_id)), True

    def publish(self, note_id, expected_version):
        with self.database.connect(write=True) as connection:
            row = entry(connection, note_id)
            require_version(row, expected_version)
            if row["published_revision"] == row["latest_revision"]:
                return local_view(connection, row)
            now = int(self.clock())
            sequence = row["sequence"]
            if sequence is None:
                sequence = next_sequence(connection)
            connection.execute(
                "UPDATE public_notes SET published_revision=latest_revision,sequence=?,"
                "published_at=coalesce(published_at,?),public_updated_at=?,"
                "updated_at=?,version=version+1 WHERE id=?",
                (sequence, now, now, now, row["id"]),
            )
            return local_view(connection, entry(connection, row["id"]))

    def withdraw(self, note_id, expected_version):
        with self.database.connect(write=True) as connection:
            row = entry(connection, note_id)
            require_version(row, expected_version)
            if row["published_revision"] is None:
                return local_view(connection, row)
            connection.execute(
                "UPDATE public_notes SET published_revision=NULL,updated_at=?,version=version+1 "
                "WHERE id=?",
                (int(self.clock()), row["id"]),
            )
            return local_view(connection, entry(connection, row["id"]))

    def list_notes(self, *, limit=10, before=None):
        if not 1 <= limit <= 30 or (before is not None and before < 1):
            raise ValueError("Invalid public history page")
        clause, params = ("", []) if before is None else (" AND n.sequence<?", [before])
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {PUBLIC_COLUMNS} FROM public_notes n JOIN public_note_revisions r "
                "ON r.note_id=n.id AND r.revision=n.published_revision "
                f"WHERE n.published_revision IS NOT NULL{clause} "
                "ORDER BY n.sequence DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
        notes = [dict(row) for row in rows[:limit]]
        return {
            "notes": notes,
            "next_before": notes[-1]["sequence"] if len(rows) > limit else None,
        }

    def get(self, note_id):
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {PUBLIC_COLUMNS},r.body FROM public_notes n JOIN public_note_revisions r "
                "ON r.note_id=n.id AND r.revision=n.published_revision WHERE n.id=?",
                (str(UUID(note_id)),),
            ).fetchone()
        if row is None:
            raise ApiError(404, "note_not_found", "这条近况当前不可读取。")
        return dict(row)

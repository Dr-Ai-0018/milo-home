CREATE TABLE public_notes (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version >= 1),
    latest_revision INTEGER NOT NULL CHECK (latest_revision >= 1),
    published_revision INTEGER,
    sequence INTEGER UNIQUE CHECK (sequence >= 1),
    source_key TEXT UNIQUE,
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
    published_at INTEGER,
    public_updated_at INTEGER,
    FOREIGN KEY (id, latest_revision) REFERENCES public_note_revisions(note_id, revision)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (id, published_revision) REFERENCES public_note_revisions(note_id, revision)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK ((sequence IS NULL) = (published_at IS NULL)),
    CHECK ((published_at IS NULL) = (public_updated_at IS NULL)),
    CHECK (published_revision IS NULL OR sequence IS NOT NULL),
    CHECK (published_revision IS NULL OR published_revision <= latest_revision),
    CHECK (published_at IS NULL OR published_at >= created_at),
    CHECK (public_updated_at IS NULL OR public_updated_at >= published_at)
);

CREATE TABLE public_note_revisions (
    note_id TEXT NOT NULL REFERENCES public_notes(id) DEFERRABLE INITIALLY DEFERRED,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 120),
    summary TEXT NOT NULL CHECK (length(trim(summary)) BETWEEN 1 AND 240),
    body TEXT NOT NULL CHECK (length(trim(body)) BETWEEN 1 AND 12000),
    category TEXT CHECK (category IS NULL OR length(trim(category)) BETWEEN 1 AND 40),
    content_date TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    PRIMARY KEY (note_id, revision)
);

CREATE TRIGGER public_note_revision_immutable
BEFORE UPDATE ON public_note_revisions
BEGIN
    SELECT RAISE(ABORT, 'Published content revisions are immutable');
END;

CREATE TRIGGER public_note_revision_no_delete
BEFORE DELETE ON public_note_revisions
BEGIN
    SELECT RAISE(ABORT, 'Content revisions cannot be deleted');
END;

CREATE TRIGGER public_note_first_publication_immutable
BEFORE UPDATE OF sequence, published_at ON public_notes
WHEN OLD.sequence IS NOT NULL AND (
    NEW.sequence IS NOT OLD.sequence OR NEW.published_at IS NOT OLD.published_at
)
BEGIN
    SELECT RAISE(ABORT, 'First publication identity is immutable');
END;

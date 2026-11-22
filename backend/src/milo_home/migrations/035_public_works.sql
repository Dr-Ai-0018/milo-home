CREATE TABLE public_works (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version >= 1),
    latest_revision INTEGER NOT NULL CHECK (latest_revision >= 1),
    published_revision INTEGER,
    sequence INTEGER UNIQUE CHECK (sequence >= 1),
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
    published_at INTEGER,
    public_updated_at INTEGER,
    FOREIGN KEY (id, latest_revision) REFERENCES public_work_revisions(work_id, revision)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (id, published_revision) REFERENCES public_work_revisions(work_id, revision)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK ((sequence IS NULL) = (published_at IS NULL)),
    CHECK ((published_at IS NULL) = (public_updated_at IS NULL)),
    CHECK (published_revision IS NULL OR sequence IS NOT NULL),
    CHECK (published_revision IS NULL OR published_revision <= latest_revision),
    CHECK (published_at IS NULL OR published_at >= created_at),
    CHECK (public_updated_at IS NULL OR public_updated_at >= published_at)
);

CREATE TABLE public_work_revisions (
    work_id TEXT NOT NULL REFERENCES public_works(id) DEFERRABLE INITIALLY DEFERRED,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 120),
    summary TEXT NOT NULL CHECK (length(trim(summary)) BETWEEN 1 AND 240),
    body TEXT NOT NULL CHECK (length(trim(body)) BETWEEN 1 AND 12000),
    category TEXT CHECK (category IS NULL OR length(trim(category)) BETWEEN 1 AND 40),
    version_label TEXT CHECK (version_label IS NULL OR length(trim(version_label)) BETWEEN 1 AND 60),
    content_date TEXT NOT NULL,
    credits TEXT CHECK (credits IS NULL OR length(trim(credits)) BETWEEN 1 AND 500),
    links_json TEXT NOT NULL CHECK (json_valid(links_json) AND json_type(links_json) = 'array'
        AND json_array_length(links_json) <= 4),
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    PRIMARY KEY (work_id, revision)
);

CREATE TABLE public_work_assets (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES public_works(id),
    filename TEXT NOT NULL CHECK (length(filename) BETWEEN 1 AND 160),
    size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 0 AND 33554432),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    UNIQUE (work_id, id)
);

CREATE TABLE public_work_revision_assets (
    work_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('download', 'cover')),
    position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 15),
    alt TEXT,
    width INTEGER,
    height INTEGER,
    PRIMARY KEY (work_id, revision, role, position),
    UNIQUE (work_id, revision, role, asset_id),
    FOREIGN KEY (work_id, revision) REFERENCES public_work_revisions(work_id, revision),
    FOREIGN KEY (work_id, asset_id) REFERENCES public_work_assets(work_id, id),
    CHECK ((role = 'download' AND alt IS NULL AND width IS NULL AND height IS NULL)
        OR (role = 'cover' AND position = 0 AND alt IS NOT NULL
            AND width IS NOT NULL AND height IS NOT NULL
            AND length(trim(alt)) BETWEEN 1 AND 240
            AND width > 0 AND height > 0 AND width * height <= 16000000))
);

CREATE INDEX public_work_asset_references ON public_work_revision_assets (work_id, asset_id);

CREATE TRIGGER public_work_revision_immutable BEFORE UPDATE ON public_work_revisions
BEGIN
    SELECT RAISE(ABORT, 'Work revisions are immutable');
END;
CREATE TRIGGER public_work_revision_no_delete BEFORE DELETE ON public_work_revisions
BEGIN
    SELECT RAISE(ABORT, 'Work revisions cannot be deleted');
END;
CREATE TRIGGER public_work_asset_immutable BEFORE UPDATE ON public_work_assets
BEGIN
    SELECT RAISE(ABORT, 'Work assets are immutable');
END;
CREATE TRIGGER public_work_asset_no_delete BEFORE DELETE ON public_work_assets
BEGIN
    SELECT RAISE(ABORT, 'Work assets cannot be deleted');
END;
CREATE TRIGGER public_work_selection_immutable BEFORE UPDATE ON public_work_revision_assets
BEGIN
    SELECT RAISE(ABORT, 'Work asset selections are immutable');
END;
CREATE TRIGGER public_work_selection_no_delete BEFORE DELETE ON public_work_revision_assets
BEGIN
    SELECT RAISE(ABORT, 'Work asset selections cannot be deleted');
END;
CREATE TRIGGER public_work_selection_no_append BEFORE INSERT ON public_work_revision_assets
WHEN EXISTS (SELECT 1 FROM public_works WHERE id = NEW.work_id
    AND latest_revision >= NEW.revision)
BEGIN
    SELECT RAISE(ABORT, 'Completed work revisions cannot gain assets');
END;
CREATE TRIGGER public_work_first_publication_immutable
BEFORE UPDATE OF sequence, published_at ON public_works
WHEN OLD.sequence IS NOT NULL AND (
    NEW.sequence IS NOT OLD.sequence OR NEW.published_at IS NOT OLD.published_at
)
BEGIN
    SELECT RAISE(ABORT, 'First work publication identity is immutable');
END;
CREATE TRIGGER public_work_no_delete BEFORE DELETE ON public_works
BEGIN
    SELECT RAISE(ABORT, 'Works retain their publication identities');
END;

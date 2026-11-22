CREATE TABLE reception_changes (
    owner_id TEXT NOT NULL REFERENCES users(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    scope TEXT NOT NULL CHECK (scope IN ('reply', 'work', 'all')),
    mode TEXT NOT NULL CHECK (mode IN ('available', 'focusing', 'resting', 'later')),
    resume_on TEXT CHECK (resume_on IN ('at_time', 'new_input', 'explicit')),
    until INTEGER,
    updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
    recorded_at INTEGER NOT NULL CHECK (recorded_at >= updated_at),
    cause TEXT NOT NULL CHECK (cause IN ('control', 'result', 'at_time', 'new_input')),
    origin_run_id TEXT UNIQUE REFERENCES runs(id),
    applied INTEGER NOT NULL CHECK (applied IN (0, 1)),
    PRIMARY KEY (owner_id, revision),
    CHECK ((cause = 'result') = (origin_run_id IS NOT NULL)),
    CHECK (mode != 'available' OR (resume_on IS NULL AND until IS NULL)),
    CHECK (mode = 'available' OR (resume_on IS NOT NULL AND
        ((resume_on = 'at_time' AND until > updated_at) OR
         (resume_on IN ('new_input', 'explicit') AND until IS NULL))))
);
CREATE TRIGGER reception_changes_scope BEFORE INSERT ON reception_changes
BEGIN
    SELECT RAISE(ABORT, 'Reception revision mismatch') WHERE NEW.revision != (
        SELECT coalesce(max(revision), 0) + 1 FROM reception_changes WHERE owner_id = NEW.owner_id
    );
    SELECT RAISE(ABORT, 'Reception result owner mismatch')
    WHERE NEW.origin_run_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM runs JOIN threads ON threads.id = runs.thread_id
        WHERE runs.id = NEW.origin_run_id AND threads.owner_id = NEW.owner_id
    );
END;
CREATE TRIGGER reception_changes_immutable BEFORE UPDATE ON reception_changes
BEGIN
    SELECT RAISE(ABORT, 'Reception birth evidence is immutable');
END;

CREATE TABLE reception_slots (
    owner_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('reply', 'work')),
    revision INTEGER NOT NULL,
    PRIMARY KEY (owner_id, kind),
    FOREIGN KEY (owner_id, revision) REFERENCES reception_changes(owner_id, revision)
);
CREATE TRIGGER reception_slots_insert BEFORE INSERT ON reception_slots
BEGIN
    SELECT RAISE(ABORT, 'Reception slot scope mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM reception_changes WHERE owner_id = NEW.owner_id AND revision = NEW.revision
            AND scope IN (NEW.kind, 'all') AND applied = 1
    );
END;
CREATE TRIGGER reception_slots_update BEFORE UPDATE ON reception_slots
BEGIN
    SELECT RAISE(ABORT, 'Reception slot scope mismatch') WHERE
        NEW.owner_id != OLD.owner_id OR NEW.kind != OLD.kind OR NEW.revision <= OLD.revision
        OR NOT EXISTS (SELECT 1 FROM reception_changes
            WHERE owner_id = NEW.owner_id AND revision = NEW.revision
                AND scope IN (NEW.kind, 'all') AND applied = 1);
END;

CREATE TABLE reception_run_bases (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    reply_revision INTEGER NOT NULL CHECK (reply_revision >= 0),
    work_revision INTEGER NOT NULL CHECK (work_revision >= 0),
    reply_input INTEGER NOT NULL CHECK (reply_input >= 0),
    work_input INTEGER NOT NULL CHECK (work_input >= 0)
);
CREATE TRIGGER reception_run_bases_immutable BEFORE UPDATE ON reception_run_bases
BEGIN
    SELECT RAISE(ABORT, 'Reception launch baseline is immutable');
END;

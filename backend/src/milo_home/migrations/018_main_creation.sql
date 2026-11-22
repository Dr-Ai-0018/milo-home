CREATE TABLE work_main_creations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE REFERENCES work_sessions(id),
    choice_digest TEXT NOT NULL UNIQUE CHECK (length(choice_digest) = 64),
    owner_revision INTEGER NOT NULL,
    workspace_revision INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    deadline_at INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held', 'unknown', 'closed')),
    CHECK (deadline_at > created_at AND deadline_at <= created_at + 3600)
);
CREATE UNIQUE INDEX main_creation_single_active ON work_main_creations((1)) WHERE state!='closed';
CREATE TRIGGER main_creation_identity_immutable
BEFORE UPDATE OF id,session_id,choice_digest,owner_revision,workspace_revision,created_at,deadline_at
ON work_main_creations
BEGIN
    SELECT RAISE(ABORT, 'main creation identity is immutable');
END;
CREATE TRIGGER main_creation_no_delete BEFORE DELETE ON work_main_creations
BEGIN
    SELECT RAISE(ABORT, 'main creation cannot be deleted');
END;
CREATE TRIGGER main_creation_no_reopen BEFORE UPDATE OF state ON work_main_creations
WHEN OLD.state='closed' AND NEW.state!='closed'
BEGIN
    SELECT RAISE(ABORT, 'main creation cannot reopen');
END;
CREATE TRIGGER main_creation_blocks_legacy_insert BEFORE INSERT ON runs
WHEN NEW.status IN ('queued','running','stopping')
AND EXISTS (SELECT 1 FROM work_main_creations WHERE state!='closed')
BEGIN
    SELECT RAISE(ABORT, 'main creation already occupied');
END;
CREATE TRIGGER main_creation_blocks_legacy_update BEFORE UPDATE OF status ON runs
WHEN NEW.status IN ('queued','running','stopping')
AND EXISTS (SELECT 1 FROM work_main_creations WHERE state!='closed')
BEGIN
    SELECT RAISE(ABORT, 'main creation already occupied');
END;
CREATE TRIGGER main_creation_blocks_work_insert BEFORE INSERT ON execution_windows
WHEN NEW.state!='closed'
AND EXISTS (SELECT 1 FROM work_main_creations WHERE state!='closed')
BEGIN
    SELECT RAISE(ABORT, 'main creation already occupied');
END;
CREATE TRIGGER main_creation_blocks_work_update BEFORE UPDATE OF state ON execution_windows
WHEN NEW.state!='closed'
AND EXISTS (SELECT 1 FROM work_main_creations WHERE state!='closed')
BEGIN
    SELECT RAISE(ABORT, 'main creation already occupied');
END;
CREATE TRIGGER execution_blocks_main_creation BEFORE INSERT ON work_main_creations
WHEN EXISTS (SELECT 1 FROM runs WHERE status IN ('queued','running','stopping'))
OR EXISTS (SELECT 1 FROM execution_windows WHERE state!='closed')
BEGIN
    SELECT RAISE(ABORT, 'execution already occupied');
END;

-- The v18 table enforced a single attempt forever. Preserve every original
-- value and keep existing closed rows unclassified until evidence is imported.
DROP TRIGGER main_creation_blocks_legacy_insert;
DROP TRIGGER main_creation_blocks_legacy_update;
DROP TRIGGER main_creation_blocks_work_insert;
DROP TRIGGER main_creation_blocks_work_update;

ALTER TABLE work_main_creations RENAME TO work_main_creations_v18;

CREATE TABLE work_main_creations (
    id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES work_sessions(id),
    choice_digest TEXT NOT NULL UNIQUE CHECK (length(choice_digest) = 64),
    owner_revision INTEGER NOT NULL,
    workspace_revision INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    deadline_at INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held', 'unknown', 'closed')),
    supersedes_id TEXT UNIQUE REFERENCES work_main_creations(id),
    authorization_id TEXT UNIQUE REFERENCES work_main_attempt_grants(id),
    CHECK ((supersedes_id IS NULL) = (authorization_id IS NULL)),
    CHECK (deadline_at > created_at AND deadline_at <= created_at + 3600)
);

CREATE TABLE work_main_attempt_retirements (
    attempt_id TEXT NOT NULL PRIMARY KEY REFERENCES work_main_creations(id),
    evidence_digest TEXT NOT NULL CHECK (
        length(evidence_digest) = 64 AND evidence_digest NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE TABLE work_main_attempt_grants (
    id TEXT NOT NULL PRIMARY KEY,
    predecessor_id TEXT NOT NULL UNIQUE REFERENCES work_main_attempt_retirements(attempt_id),
    new_choice_digest TEXT NOT NULL UNIQUE CHECK (
        length(new_choice_digest) = 64 AND new_choice_digest NOT GLOB '*[^0-9a-f]*'
    )
);

INSERT INTO work_main_creations (
    rowid, id, session_id, choice_digest, owner_revision, workspace_revision,
    created_at, deadline_at, state
)
SELECT
    rowid, id, session_id, choice_digest, owner_revision, workspace_revision,
    created_at, deadline_at, state
FROM work_main_creations_v18;
DROP TABLE work_main_creations_v18;

CREATE UNIQUE INDEX main_creation_single_active ON work_main_creations((1)) WHERE state!='closed';
CREATE UNIQUE INDEX main_creation_one_root ON work_main_creations(session_id) WHERE supersedes_id IS NULL;

CREATE TRIGGER main_creation_identity_immutable
BEFORE UPDATE OF id,session_id,choice_digest,owner_revision,workspace_revision,created_at,deadline_at,
    supersedes_id,authorization_id,rowid,_rowid_,oid
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

-- A conflict must not delete historical rows via INSERT OR REPLACE.
CREATE TRIGGER main_creation_no_replace BEFORE INSERT ON work_main_creations
WHEN EXISTS (
    SELECT 1 FROM work_main_creations AS old
    WHERE old.rowid=NEW.rowid OR old.id=NEW.id OR old.choice_digest=NEW.choice_digest
    OR old.supersedes_id=NEW.supersedes_id OR old.authorization_id=NEW.authorization_id
    OR (old.session_id=NEW.session_id AND old.supersedes_id IS NULL AND NEW.supersedes_id IS NULL)
    OR (old.state!='closed' AND NEW.state!='closed')
)
BEGIN
    SELECT RAISE(ABORT, 'main creation history or occupancy already exists');
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

CREATE TRIGGER main_creation_successor_authorized BEFORE INSERT ON work_main_creations
WHEN EXISTS (SELECT 1 FROM work_runtime_bindings WHERE session_id=NEW.session_id)
OR (NEW.supersedes_id IS NOT NULL AND (
    NEW.state!='held' OR NOT EXISTS (
        SELECT 1 FROM work_main_creations AS old
        JOIN work_main_attempt_retirements AS retired ON retired.attempt_id=old.id
        JOIN work_main_attempt_grants AS grant ON grant.predecessor_id=retired.attempt_id
        WHERE old.id=NEW.supersedes_id AND old.session_id=NEW.session_id AND old.state='closed'
        AND grant.id=NEW.authorization_id AND grant.new_choice_digest=NEW.choice_digest
    )
))
BEGIN
    SELECT RAISE(ABORT, 'main creation successor unauthorized');
END;

CREATE TRIGGER main_attempt_retirement_insert BEFORE INSERT ON work_main_attempt_retirements
WHEN EXISTS (
    SELECT 1 FROM work_main_attempt_retirements
    WHERE rowid=NEW.rowid OR attempt_id=NEW.attempt_id
)
OR NOT EXISTS (
    SELECT 1 FROM work_main_creations AS old
    WHERE old.id=NEW.attempt_id AND old.state='closed'
    AND NOT EXISTS (SELECT 1 FROM work_runtime_bindings WHERE session_id=old.session_id)
)
BEGIN
    SELECT RAISE(ABORT, 'main creation retirement requires an unbound closed attempt');
END;
CREATE TRIGGER main_attempt_retirement_no_update BEFORE UPDATE ON work_main_attempt_retirements
BEGIN
    SELECT RAISE(ABORT, 'main creation retirement is immutable');
END;
CREATE TRIGGER main_attempt_retirement_no_delete BEFORE DELETE ON work_main_attempt_retirements
BEGIN
    SELECT RAISE(ABORT, 'main creation retirement cannot be deleted');
END;

CREATE TRIGGER main_attempt_grant_insert BEFORE INSERT ON work_main_attempt_grants
WHEN EXISTS (
    SELECT 1 FROM work_main_attempt_grants
    WHERE rowid=NEW.rowid OR id=NEW.id OR predecessor_id=NEW.predecessor_id
    OR new_choice_digest=NEW.new_choice_digest
)
OR NOT EXISTS (
    SELECT 1 FROM work_main_attempt_retirements AS retired
    JOIN work_main_creations AS old ON old.id=retired.attempt_id
    WHERE retired.attempt_id=NEW.predecessor_id
    AND NOT EXISTS (SELECT 1 FROM work_runtime_bindings WHERE session_id=old.session_id)
)
BEGIN
    SELECT RAISE(ABORT, 'main creation grant requires an unbound retired attempt');
END;
CREATE TRIGGER main_attempt_grant_no_update BEFORE UPDATE ON work_main_attempt_grants
BEGIN
    SELECT RAISE(ABORT, 'main creation grant is immutable');
END;
CREATE TRIGGER main_attempt_grant_no_delete BEFORE DELETE ON work_main_attempt_grants
BEGIN
    SELECT RAISE(ABORT, 'main creation grant cannot be deleted');
END;

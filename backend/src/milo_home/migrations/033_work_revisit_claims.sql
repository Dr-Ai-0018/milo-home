CREATE TABLE work_decision_capabilities (
    owner_id TEXT PRIMARY KEY REFERENCES users(id),
    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
    updated_at INTEGER NOT NULL
);
CREATE TABLE work_revisit_claims (
    window_id TEXT PRIMARY KEY REFERENCES execution_windows(id),
    revisit_id TEXT NOT NULL REFERENCES work_revisits(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    input_id TEXT NOT NULL UNIQUE REFERENCES work_inputs(id),
    decision_revision INTEGER NOT NULL CHECK (decision_revision >= 1),
    due_at INTEGER NOT NULL,
    claimed_at INTEGER NOT NULL,
    UNIQUE (revisit_id, revision)
);
CREATE TRIGGER work_revisit_claim_scope BEFORE INSERT ON work_revisit_claims
WHEN NOT EXISTS (
    SELECT 1 FROM execution_windows w JOIN work_revisits v ON v.task_id=w.task_id
    JOIN work_task_decisions d ON d.task_id=w.task_id AND d.revisit_id=v.id
    JOIN work_inputs i ON i.task_id=w.task_id
    WHERE w.id=NEW.window_id AND w.created_at=NEW.claimed_at
        AND v.id=NEW.revisit_id AND v.revision=NEW.revision
        AND d.revision=NEW.decision_revision AND d.state='deferred'
        AND i.id=NEW.input_id AND i.delivery='queued' AND v.due_at=NEW.due_at
        AND v.due_at<=NEW.claimed_at AND v.state IN ('scheduled','blocked')
)
BEGIN
    SELECT RAISE(ABORT, 'Work revisit claim source mismatch');
END;
CREATE TRIGGER work_revisit_claim_immutable BEFORE UPDATE ON work_revisit_claims
BEGIN
    SELECT RAISE(ABORT, 'Work revisit claim birth evidence is immutable');
END;

CREATE TABLE work_window_facts (
    window_id TEXT PRIMARY KEY REFERENCES execution_windows(id),
    payload TEXT NOT NULL
);
CREATE TRIGGER work_window_facts_immutable BEFORE UPDATE ON work_window_facts
BEGIN
    SELECT RAISE(ABORT, 'Work window historical facts are immutable');
END;

CREATE TABLE work_revisits (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES work_tasks(id),
    origin_result_id TEXT NOT NULL UNIQUE REFERENCES work_results(id),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    state TEXT NOT NULL CHECK (state IN (
        'scheduled', 'running', 'blocked', 'needs_reconciliation', 'completed', 'cancelled'
    )),
    due_at INTEGER NOT NULL,
    initial_due_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    blocked_reason TEXT CHECK (blocked_reason IN (
        'schedule', 'reception', 'budget', 'service', 'occupancy'
    )),
    CHECK ((state = 'blocked') = (blocked_reason IS NOT NULL)),
    CHECK (initial_due_at > created_at)
);
CREATE INDEX work_revisits_due ON work_revisits(state, due_at, id);
CREATE TRIGGER work_revisit_scope BEFORE INSERT ON work_revisits
WHEN NOT EXISTS (
    SELECT 1 FROM work_results WHERE id=NEW.origin_result_id AND task_id=NEW.task_id
        AND outcome='completed' AND observed_at=NEW.created_at
)
BEGIN
    SELECT RAISE(ABORT, 'Work revisit source mismatch');
END;
CREATE TRIGGER work_revisit_identity BEFORE UPDATE OF
    id, task_id, origin_result_id, initial_due_at, created_at ON work_revisits
BEGIN
    SELECT RAISE(ABORT, 'Work revisit birth evidence is immutable');
END;

CREATE TABLE work_decisions (
    task_id TEXT NOT NULL REFERENCES work_tasks(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    result_id TEXT NOT NULL UNIQUE REFERENCES work_results(id),
    state TEXT NOT NULL CHECK (state IN ('accepted', 'deferred', 'declined', 'needs_clarification')),
    receipt TEXT NOT NULL CHECK (length(trim(receipt)) BETWEEN 1 AND 8000),
    decided_at INTEGER NOT NULL,
    revisit_id TEXT UNIQUE REFERENCES work_revisits(id),
    CHECK ((state='deferred') = (revisit_id IS NOT NULL)),
    PRIMARY KEY (task_id, revision)
);
CREATE TRIGGER work_decision_scope BEFORE INSERT ON work_decisions
BEGIN
    SELECT RAISE(ABORT, 'Work decision source mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM work_results r JOIN execution_windows w ON w.id=r.window_id
        WHERE r.id=NEW.result_id AND r.task_id=NEW.task_id AND r.outcome='completed'
            AND r.observed_at=NEW.decided_at AND w.state='closed' AND w.stop_reason IS NULL
    );
    SELECT RAISE(ABORT, 'Work decision revision mismatch') WHERE NEW.revision != (
        SELECT coalesce(max(revision),0)+1 FROM work_decisions WHERE task_id=NEW.task_id
    );
    SELECT RAISE(ABORT, 'Work decision revisit mismatch')
    WHERE NEW.revisit_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM work_revisits WHERE id=NEW.revisit_id
            AND task_id=NEW.task_id AND origin_result_id=NEW.result_id
    );
END;
CREATE TRIGGER work_decision_immutable BEFORE UPDATE ON work_decisions
BEGIN
    SELECT RAISE(ABORT, 'Work decision history is immutable');
END;
CREATE TRIGGER work_decision_no_delete BEFORE DELETE ON work_decisions
BEGIN
    SELECT RAISE(ABORT, 'Work decision history cannot be deleted');
END;
CREATE VIEW work_task_decisions AS
SELECT * FROM work_decisions d WHERE revision=(
    SELECT max(revision) FROM work_decisions WHERE task_id=d.task_id
);

CREATE TABLE work_continuations (
    input_id TEXT PRIMARY KEY REFERENCES work_inputs(id),
    task_id TEXT NOT NULL,
    decision_revision INTEGER NOT NULL,
    FOREIGN KEY (task_id, decision_revision) REFERENCES work_decisions(task_id, revision)
);
CREATE TRIGGER work_continuation_scope BEFORE INSERT ON work_continuations
WHEN NOT EXISTS (
    SELECT 1 FROM work_inputs i JOIN work_task_decisions d ON d.task_id=i.task_id
    WHERE i.id=NEW.input_id AND i.task_id=NEW.task_id AND i.kind='supplement'
        AND d.revision=NEW.decision_revision AND d.state='needs_clarification'
        AND i.created_at>=d.decided_at
)
BEGIN
    SELECT RAISE(ABORT, 'Work clarification source mismatch');
END;
CREATE TRIGGER work_continuation_immutable BEFORE UPDATE ON work_continuations
BEGIN
    SELECT RAISE(ABORT, 'Work clarification source is immutable');
END;

CREATE TABLE work_revisit_inputs (
    input_id TEXT PRIMARY KEY REFERENCES work_inputs(id),
    revisit_id TEXT NOT NULL REFERENCES work_revisits(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    due_at INTEGER NOT NULL,
    UNIQUE (revisit_id, revision)
);
CREATE TRIGGER work_revisit_input_scope BEFORE INSERT ON work_revisit_inputs
WHEN NOT EXISTS (
    SELECT 1 FROM work_inputs i JOIN work_revisits v ON v.task_id=i.task_id
    WHERE i.id=NEW.input_id AND v.id=NEW.revisit_id AND v.revision=NEW.revision
        AND v.due_at=NEW.due_at AND v.due_at<=i.created_at AND i.kind='supplement'
)
BEGIN
    SELECT RAISE(ABORT, 'Work revisit input source mismatch');
END;
CREATE TRIGGER work_revisit_input_immutable BEFORE UPDATE ON work_revisit_inputs
BEGIN
    SELECT RAISE(ABORT, 'Work revisit input source is immutable');
END;

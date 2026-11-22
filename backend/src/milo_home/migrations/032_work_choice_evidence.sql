CREATE TABLE work_turn_bases (
    permit_id TEXT PRIMARY KEY REFERENCES execution_permits(id),
    reply_revision INTEGER NOT NULL CHECK (reply_revision >= 0),
    work_revision INTEGER NOT NULL CHECK (work_revision >= 0),
    reply_input INTEGER NOT NULL CHECK (reply_input >= 0),
    work_input INTEGER NOT NULL CHECK (work_input >= 0)
);
CREATE TRIGGER work_turn_bases_immutable BEFORE UPDATE ON work_turn_bases
BEGIN
    SELECT RAISE(ABORT, 'Work reception launch baseline is immutable');
END;

CREATE TABLE work_choice_receipts (
    result_id TEXT PRIMARY KEY REFERENCES work_results(id),
    payload TEXT NOT NULL,
    applied INTEGER NOT NULL CHECK (applied IN (0,1))
);
CREATE TRIGGER work_choice_receipt_immutable BEFORE UPDATE ON work_choice_receipts
BEGIN
    SELECT RAISE(ABORT, 'Original work choice receipt is immutable');
END;

CREATE TABLE work_result_receptions (
    result_id TEXT PRIMARY KEY REFERENCES work_results(id),
    owner_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    FOREIGN KEY (owner_id, revision) REFERENCES reception_changes(owner_id, revision),
    UNIQUE (owner_id, revision)
);
CREATE TRIGGER work_result_reception_scope BEFORE INSERT ON work_result_receptions
WHEN NOT EXISTS (
    SELECT 1 FROM work_results r JOIN work_tasks t ON t.id=r.task_id
    JOIN reception_changes c ON c.owner_id=t.owner_id
    WHERE r.id=NEW.result_id AND t.owner_id=NEW.owner_id AND c.revision=NEW.revision
        AND c.updated_at=r.observed_at AND c.cause='control' AND c.origin_run_id IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'Work reception result source mismatch');
END;
CREATE TRIGGER work_result_reception_immutable BEFORE UPDATE ON work_result_receptions
BEGIN
    SELECT RAISE(ABORT, 'Work reception origin is immutable');
END;

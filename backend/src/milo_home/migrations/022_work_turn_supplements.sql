CREATE TABLE work_steer_turns (
    permit_id TEXT PRIMARY KEY REFERENCES execution_permits(id),
    task_id TEXT NOT NULL REFERENCES work_tasks(id),
    accepting INTEGER NOT NULL CHECK (accepting IN (0, 1))
);
CREATE UNIQUE INDEX work_steer_one_active ON work_steer_turns(task_id) WHERE accepting=1;
CREATE TRIGGER work_steer_turn_scope BEFORE INSERT ON work_steer_turns
WHEN NOT EXISTS (
    SELECT 1 FROM execution_permits p
    JOIN execution_windows w ON w.id=p.window_id
    JOIN work_inputs i ON i.id=p.input_id
    WHERE p.id=NEW.permit_id AND p.state='launched' AND w.state='running'
        AND w.task_id=NEW.task_id AND i.delivery='acknowledged'
)
BEGIN
    SELECT RAISE(ABORT, 'supplement target must be an acknowledged live turn');
END;
CREATE TRIGGER work_steer_turn_no_reopen
BEFORE UPDATE ON work_steer_turns
WHEN NEW.permit_id!=OLD.permit_id OR NEW.task_id!=OLD.task_id OR NEW.accepting>OLD.accepting
BEGIN
    SELECT RAISE(ABORT, 'supplement target cannot change or reopen');
END;

CREATE TABLE work_steer_inputs (
    input_id TEXT PRIMARY KEY REFERENCES work_inputs(id),
    permit_id TEXT NOT NULL REFERENCES work_steer_turns(permit_id)
);
CREATE INDEX work_steer_inputs_permit ON work_steer_inputs(permit_id);
CREATE TRIGGER work_steer_input_scope BEFORE INSERT ON work_steer_inputs
WHEN NOT EXISTS (
    SELECT 1 FROM work_steer_turns t JOIN work_inputs i ON i.task_id=t.task_id
    WHERE t.permit_id=NEW.permit_id AND t.accepting=1 AND i.id=NEW.input_id
        AND i.kind='supplement' AND i.delivery='queued'
)
BEGIN
    SELECT RAISE(ABORT, 'supplement input differs from live turn');
END;
CREATE TRIGGER work_steer_input_immutable BEFORE UPDATE ON work_steer_inputs
BEGIN
    SELECT RAISE(ABORT, 'supplement target is immutable');
END;
CREATE TRIGGER work_steer_no_start BEFORE INSERT ON execution_permits
WHEN EXISTS (SELECT 1 FROM work_steer_inputs WHERE input_id=NEW.input_id)
BEGIN
    SELECT RAISE(ABORT, 'same-turn supplement cannot start a turn');
END;

CREATE TRIGGER work_steer_cancel_unsent AFTER UPDATE OF accepting ON work_steer_turns
WHEN OLD.accepting=1 AND NEW.accepting=0
BEGIN
    UPDATE work_inputs SET delivery='cancelled' WHERE delivery='queued' AND id IN (
        SELECT input_id FROM work_steer_inputs WHERE permit_id=NEW.permit_id
    );
END;
CREATE TRIGGER work_steer_permit_end AFTER UPDATE OF state ON execution_permits
WHEN OLD.state='launched' AND NEW.state!='launched'
BEGIN
    UPDATE work_steer_turns SET accepting=0 WHERE permit_id=NEW.id AND accepting=1;
    UPDATE work_inputs SET delivery='unknown' WHERE delivery='sending' AND id IN (
        SELECT input_id FROM work_steer_inputs WHERE permit_id=NEW.id
    );
END;
CREATE TRIGGER work_steer_window_end AFTER UPDATE OF state ON execution_windows
WHEN OLD.state='running' AND NEW.state!='running'
BEGIN
    UPDATE work_steer_turns SET accepting=0 WHERE task_id=NEW.task_id AND accepting=1;
END;
CREATE TRIGGER work_steer_window_unknown AFTER UPDATE OF state ON execution_windows
WHEN OLD.state!=NEW.state AND NEW.state='unknown'
BEGIN
    UPDATE work_inputs SET delivery='unknown' WHERE task_id=NEW.task_id AND delivery='sending'
        AND id IN (SELECT input_id FROM work_steer_inputs);
END;

CREATE TABLE work_results (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES work_tasks(id),
    input_id TEXT NOT NULL UNIQUE REFERENCES work_inputs(id),
    permit_id TEXT NOT NULL UNIQUE REFERENCES execution_permits(id),
    window_id TEXT NOT NULL REFERENCES execution_windows(id),
    stream_id TEXT NOT NULL,
    receipt_sequence INTEGER NOT NULL CHECK (receipt_sequence > 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('completed', 'failed', 'interrupted')),
    observed_at INTEGER NOT NULL CHECK (observed_at >= 0),
    UNIQUE (stream_id, receipt_sequence)
);
CREATE TRIGGER work_result_scope BEFORE INSERT ON work_results
WHEN NOT EXISTS (
    SELECT 1 FROM execution_permits p
    JOIN execution_windows w ON w.id = p.window_id
    JOIN execution_start_intents s ON s.window_id = w.id
    JOIN work_inputs i ON i.id = p.input_id
    WHERE p.id = NEW.permit_id AND p.input_id = NEW.input_id
        AND w.id = NEW.window_id AND w.task_id = NEW.task_id
        AND i.task_id = NEW.task_id AND s.stream_id = NEW.stream_id
        AND p.launched_at IS NOT NULL AND NEW.observed_at >= p.launched_at
)
BEGIN
    SELECT RAISE(ABORT, 'work result scope mismatch');
END;
CREATE TRIGGER work_result_immutable BEFORE UPDATE ON work_results
BEGIN
    SELECT RAISE(ABORT, 'work result is immutable');
END;
CREATE TRIGGER work_result_no_delete BEFORE DELETE ON work_results
BEGIN
    SELECT RAISE(ABORT, 'work result cannot be deleted');
END;

CREATE TABLE work_events (
    task_id TEXT NOT NULL REFERENCES work_tasks(id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    kind TEXT NOT NULL CHECK (kind IN ('task_status', 'input_delivery', 'turn_result')),
    state TEXT NOT NULL,
    input_id TEXT REFERENCES work_inputs(id),
    result_id TEXT REFERENCES work_results(id),
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (task_id, sequence),
    CHECK (
        (kind = 'task_status' AND input_id IS NULL AND result_id IS NULL
            AND state IN ('queued', 'running', 'stopping', 'completed', 'failed',
                'cancelled', 'timed_out', 'needs_reconciliation'))
        OR (kind = 'input_delivery' AND input_id IS NOT NULL AND result_id IS NULL
            AND state IN ('queued', 'sending', 'acknowledged', 'unknown', 'cancelled', 'conflict'))
        OR (kind = 'turn_result' AND input_id IS NOT NULL AND result_id IS NOT NULL
            AND state IN ('completed', 'failed', 'interrupted'))
    )
);
CREATE TRIGGER work_event_scope BEFORE INSERT ON work_events
WHEN (NEW.input_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM work_inputs WHERE id = NEW.input_id AND task_id = NEW.task_id
)) OR (NEW.result_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM work_results WHERE id = NEW.result_id
        AND task_id = NEW.task_id AND input_id = NEW.input_id AND outcome = NEW.state
))
BEGIN
    SELECT RAISE(ABORT, 'work event scope mismatch');
END;
CREATE TRIGGER work_event_immutable BEFORE UPDATE ON work_events
BEGIN
    SELECT RAISE(ABORT, 'work event is immutable');
END;
CREATE TRIGGER work_event_no_delete BEFORE DELETE ON work_events
BEGIN
    SELECT RAISE(ABORT, 'work event cannot be deleted');
END;
CREATE TRIGGER work_task_created_event AFTER INSERT ON work_tasks
BEGIN
    INSERT INTO work_events VALUES (NEW.id, 1, 'task_status', NEW.status, NULL, NULL, unixepoch());
END;
CREATE TRIGGER work_task_status_event AFTER UPDATE OF status ON work_tasks
WHEN NEW.status != OLD.status
BEGIN
    INSERT INTO work_events SELECT NEW.id, coalesce(max(sequence), 0) + 1,
        'task_status', NEW.status, NULL, NULL, unixepoch()
        FROM work_events WHERE task_id = NEW.id;
END;
CREATE TRIGGER work_input_created_event AFTER INSERT ON work_inputs
BEGIN
    INSERT INTO work_events SELECT NEW.task_id, coalesce(max(sequence), 0) + 1,
        'input_delivery', NEW.delivery, NEW.id, NULL, unixepoch()
        FROM work_events WHERE task_id = NEW.task_id;
END;
CREATE TRIGGER work_input_delivery_event AFTER UPDATE OF delivery ON work_inputs
WHEN NEW.delivery != OLD.delivery
BEGIN
    INSERT INTO work_events SELECT NEW.task_id, coalesce(max(sequence), 0) + 1,
        'input_delivery', NEW.delivery, NEW.id, NULL, unixepoch()
        FROM work_events WHERE task_id = NEW.task_id;
END;
CREATE TRIGGER work_result_event AFTER INSERT ON work_results
BEGIN
    INSERT INTO work_events SELECT NEW.task_id, coalesce(max(sequence), 0) + 1,
        'turn_result', NEW.outcome, NEW.input_id, NEW.id, unixepoch()
        FROM work_events WHERE task_id = NEW.task_id;
END;

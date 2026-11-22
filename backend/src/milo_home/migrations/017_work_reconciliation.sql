CREATE TABLE work_reconciliations (
    window_id TEXT PRIMARY KEY REFERENCES execution_windows(id),
    stream_id TEXT NOT NULL,
    stop_sequence INTEGER NOT NULL CHECK (stop_sequence > 0),
    checked_at INTEGER NOT NULL CHECK (checked_at >= 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('completed','failed','cancelled','timed_out'))
);
CREATE TRIGGER work_reconciliation_scope BEFORE INSERT ON work_reconciliations
WHEN NOT EXISTS (
    SELECT 1 FROM execution_windows w JOIN execution_start_intents s ON s.window_id=w.id
    WHERE w.id=NEW.window_id AND w.state!='closed' AND s.stream_id=NEW.stream_id
        AND NEW.checked_at>=w.created_at
) OR EXISTS (
    SELECT 1 FROM execution_permits p WHERE p.window_id=NEW.window_id
        AND p.launched_at IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM work_results r WHERE r.window_id=p.window_id
                AND r.permit_id=p.id AND r.input_id=p.input_id AND r.stream_id=NEW.stream_id
                AND r.observed_at<=NEW.checked_at
        )
)
BEGIN
    SELECT RAISE(ABORT, 'reconciliation evidence scope mismatch');
END;
CREATE TRIGGER work_reconciliation_immutable BEFORE UPDATE ON work_reconciliations
BEGIN
    SELECT RAISE(ABORT, 'reconciliation is immutable');
END;
CREATE TRIGGER work_reconciliation_no_delete BEFORE DELETE ON work_reconciliations
BEGIN
    SELECT RAISE(ABORT, 'reconciliation cannot be deleted');
END;
DROP TRIGGER execution_permit_state_transition;
CREATE TRIGGER execution_permit_state_transition
BEFORE UPDATE OF state ON execution_permits WHEN NOT (
    OLD.state = NEW.state
    OR (OLD.state = 'reserved' AND NEW.state IN ('launched', 'cancelled'))
    OR (OLD.state = 'launched' AND NEW.state IN ('finished', 'unknown'))
    OR (OLD.state = 'unknown' AND NEW.state = 'finished' AND EXISTS (
        SELECT 1 FROM work_reconciliations c JOIN work_results r ON r.window_id=c.window_id
        WHERE c.window_id=OLD.window_id AND r.input_id=OLD.input_id AND r.permit_id=OLD.id
            AND r.stream_id=c.stream_id AND NEW.ended_at=c.checked_at
    ))
)
BEGIN
    SELECT RAISE(ABORT, 'execution permit cannot replay');
END;

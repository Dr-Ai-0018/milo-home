CREATE TABLE reception_returned_attempts (
    event_id TEXT PRIMARY KEY REFERENCES wake_events(id),
    run_id TEXT NOT NULL REFERENCES wake_dispatches(run_id)
);
CREATE TRIGGER reception_returned_attempts_scope BEFORE INSERT ON reception_returned_attempts
BEGIN
    SELECT RAISE(ABORT, 'Reception admission was not safely cancelled') WHERE NOT EXISTS (
        SELECT 1 FROM wake_dispatches d JOIN runs r ON r.id = d.run_id
        JOIN wake_run_events e ON e.run_id = d.run_id
        WHERE d.run_id = NEW.run_id AND e.event_id = NEW.event_id
            AND d.launched_at IS NULL AND r.status = 'cancelled'
            AND d.abort_reason IN ('reception_closed', 'reception_clock_invalid')
    );
END;

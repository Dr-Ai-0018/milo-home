CREATE TABLE execution_windows (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES work_tasks(id),
    unit_name TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    deadline_at INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held', 'running', 'stopping', 'unknown', 'closed')),
    stop_reason TEXT CHECK (stop_reason IN (
        'cancelled', 'timed_out', 'access_changed', 'schedule_closed', 'clock_rewind'
    )),
    ended_at INTEGER,
    CHECK (deadline_at > created_at AND deadline_at <= created_at + 3600),
    CHECK ((state = 'closed' AND ended_at IS NOT NULL) OR (state != 'closed' AND ended_at IS NULL))
);
CREATE UNIQUE INDEX execution_single_active ON execution_windows((1)) WHERE state != 'closed';
CREATE TRIGGER execution_window_identity_immutable
BEFORE UPDATE OF id, task_id, unit_name, created_at, deadline_at ON execution_windows
BEGIN
    SELECT RAISE(ABORT, 'execution window identity is immutable');
END;
CREATE TRIGGER execution_window_no_reopen
BEFORE UPDATE OF state ON execution_windows WHEN OLD.state = 'closed' AND NEW.state != 'closed'
BEGIN
    SELECT RAISE(ABORT, 'closed execution window cannot reopen');
END;
CREATE TRIGGER work_window_blocks_legacy_insert
BEFORE INSERT ON runs WHEN NEW.status IN ('queued', 'running', 'stopping')
    AND EXISTS (SELECT 1 FROM execution_windows WHERE state != 'closed')
BEGIN
    SELECT RAISE(ABORT, 'execution already occupied');
END;
CREATE TRIGGER work_window_blocks_legacy_update
BEFORE UPDATE OF status ON runs WHEN NEW.status IN ('queued', 'running', 'stopping')
    AND EXISTS (SELECT 1 FROM execution_windows WHERE state != 'closed')
BEGIN
    SELECT RAISE(ABORT, 'execution already occupied');
END;
CREATE TRIGGER legacy_blocks_work_insert
BEFORE INSERT ON execution_windows WHEN NEW.state != 'closed'
    AND EXISTS (SELECT 1 FROM runs WHERE status IN ('queued', 'running', 'stopping'))
BEGIN
    SELECT RAISE(ABORT, 'legacy execution already occupied');
END;
CREATE TRIGGER legacy_blocks_work_update
BEFORE UPDATE OF state ON execution_windows WHEN NEW.state != 'closed'
    AND EXISTS (SELECT 1 FROM runs WHERE status IN ('queued', 'running', 'stopping'))
BEGIN
    SELECT RAISE(ABORT, 'legacy execution already occupied');
END;

CREATE TABLE automatic_services (
    kind TEXT PRIMARY KEY CHECK (kind IN ('reply', 'work')),
    owner_id TEXT NOT NULL REFERENCES users(id),
    session_id TEXT REFERENCES work_sessions(id),
    timer_unit TEXT NOT NULL,
    database_device INTEGER NOT NULL,
    database_inode INTEGER NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    updated_at INTEGER NOT NULL,
    fault_code TEXT,
    checked_at INTEGER,
    checked_boot TEXT,
    timer_active INTEGER NOT NULL DEFAULT 0 CHECK (timer_active IN (0, 1)),
    CHECK ((kind = 'reply' AND session_id IS NULL)
        OR (kind = 'work' AND session_id IS NOT NULL))
);

CREATE TABLE reply_run_scopes (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    owner_id TEXT NOT NULL REFERENCES users(id)
);
CREATE TRIGGER reply_run_scope_immutable
BEFORE UPDATE ON reply_run_scopes
BEGIN
    SELECT RAISE(ABORT, 'reply run scope is immutable');
END;

CREATE TABLE reply_execution_results (
    run_id TEXT PRIMARY KEY REFERENCES reply_run_scopes(run_id),
    completed_at INTEGER NOT NULL,
    boot_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK (length(result) <= 262144)
);
CREATE TRIGGER reply_execution_result_immutable
BEFORE UPDATE ON reply_execution_results
BEGIN
    SELECT RAISE(ABORT, 'reply execution result is immutable');
END;

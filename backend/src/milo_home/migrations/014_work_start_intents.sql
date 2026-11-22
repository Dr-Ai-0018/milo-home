CREATE TABLE work_runtime_bindings (
    session_id TEXT PRIMARY KEY REFERENCES work_sessions(id),
    binding_id TEXT NOT NULL UNIQUE,
    fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
    created_at INTEGER NOT NULL
);
CREATE TRIGGER work_runtime_binding_immutable
BEFORE UPDATE ON work_runtime_bindings
BEGIN
    SELECT RAISE(ABORT, 'work runtime binding is immutable');
END;
CREATE TRIGGER work_runtime_binding_no_delete
BEFORE DELETE ON work_runtime_bindings
BEGIN
    SELECT RAISE(ABORT, 'work runtime binding requires reconciliation');
END;

CREATE TABLE execution_start_intents (
    window_id TEXT PRIMARY KEY REFERENCES execution_windows(id),
    binding_id TEXT NOT NULL REFERENCES work_runtime_bindings(binding_id),
    stream_id TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);
CREATE TRIGGER execution_start_intent_immutable
BEFORE UPDATE ON execution_start_intents
BEGIN
    SELECT RAISE(ABORT, 'execution start intent is immutable');
END;
CREATE TRIGGER execution_start_intent_no_delete
BEFORE DELETE ON execution_start_intents
BEGIN
    SELECT RAISE(ABORT, 'execution start intent requires reconciliation');
END;
CREATE TRIGGER execution_start_intent_scope
BEFORE INSERT ON execution_start_intents
WHEN NOT EXISTS (
    SELECT 1 FROM execution_windows
    JOIN work_tasks ON work_tasks.id = execution_windows.task_id
    JOIN work_runtime_bindings ON work_runtime_bindings.session_id = work_tasks.session_id
    WHERE execution_windows.id = NEW.window_id
        AND work_runtime_bindings.binding_id = NEW.binding_id
        AND execution_windows.state = 'held'
)
BEGIN
    SELECT RAISE(ABORT, 'execution start intent scope mismatch');
END;

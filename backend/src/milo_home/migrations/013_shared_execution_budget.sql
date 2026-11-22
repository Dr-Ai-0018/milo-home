CREATE TABLE execution_policy (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    policy TEXT,
    revision INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
INSERT INTO execution_policy VALUES (1, NULL, 0, 0);
CREATE TABLE legacy_launches (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    authorized_at INTEGER
);
CREATE TABLE execution_permits (
    id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES execution_windows(id),
    input_id TEXT NOT NULL UNIQUE REFERENCES work_inputs(id),
    created_at INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved', 'launched', 'finished', 'cancelled', 'unknown')),
    launched_at INTEGER,
    ended_at INTEGER,
    CHECK (
        (state IN ('reserved', 'cancelled') AND launched_at IS NULL)
        OR (state IN ('launched', 'finished', 'unknown') AND launched_at IS NOT NULL)
    ),
    CHECK ((state IN ('reserved', 'launched', 'unknown') AND ended_at IS NULL)
        OR (state IN ('finished', 'cancelled') AND ended_at IS NOT NULL))
);
CREATE UNIQUE INDEX execution_one_pending_turn ON execution_permits(window_id)
    WHERE state IN ('reserved', 'launched', 'unknown');
CREATE TRIGGER execution_permit_scope
BEFORE INSERT ON execution_permits WHEN NOT EXISTS (
    SELECT 1 FROM execution_windows JOIN work_inputs
    ON work_inputs.task_id = execution_windows.task_id
    WHERE execution_windows.id = NEW.window_id AND work_inputs.id = NEW.input_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution permit input scope mismatch');
END;
CREATE TRIGGER execution_permit_identity_immutable
BEFORE UPDATE OF id, window_id, input_id, created_at ON execution_permits
BEGIN
    SELECT RAISE(ABORT, 'execution permit identity is immutable');
END;
CREATE TRIGGER execution_permit_state_transition
BEFORE UPDATE OF state ON execution_permits WHEN NOT (
    OLD.state = NEW.state
    OR (OLD.state = 'reserved' AND NEW.state IN ('launched', 'cancelled'))
    OR (OLD.state = 'launched' AND NEW.state IN ('finished', 'unknown'))
)
BEGIN
    SELECT RAISE(ABORT, 'execution permit cannot replay');
END;
CREATE TRIGGER legacy_launch_identity_immutable
BEFORE UPDATE OF run_id ON legacy_launches
BEGIN
    SELECT RAISE(ABORT, 'legacy launch identity is immutable');
END;
CREATE TRIGGER legacy_launch_no_replay
BEFORE UPDATE OF authorized_at ON legacy_launches WHEN OLD.authorized_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'legacy launch cannot replay');
END;
CREATE INDEX execution_permits_window ON execution_permits(window_id);
CREATE INDEX execution_permits_day ON execution_permits(created_at);
CREATE INDEX execution_windows_day ON execution_windows(created_at);
CREATE INDEX runs_budget_day ON runs(created_at);

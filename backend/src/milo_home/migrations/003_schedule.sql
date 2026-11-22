CREATE TABLE schedule_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    policy TEXT,
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    override_phase TEXT CHECK (override_phase IN ('working', 'winding_down', 'resting')),
    override_until INTEGER,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    CHECK ((override_phase IS NULL) = (override_until IS NULL))
);
INSERT INTO schedule_state (id) VALUES (1);

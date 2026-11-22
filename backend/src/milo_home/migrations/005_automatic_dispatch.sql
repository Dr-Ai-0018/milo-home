CREATE TABLE wake_dispatches (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    token_hash TEXT NOT NULL UNIQUE,
    lease_until INTEGER NOT NULL,
    launched_at INTEGER,
    abort_reason TEXT,
    settled_at INTEGER
);

CREATE TABLE wake_event_delivery (
    event_id TEXT PRIMARY KEY REFERENCES wake_events(id),
    attempts INTEGER NOT NULL CHECK (attempts BETWEEN 1 AND 3),
    run_id TEXT REFERENCES wake_dispatches(run_id),
    retry_after INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT
);
CREATE INDEX wake_delivery_run ON wake_event_delivery(run_id);

CREATE TABLE wake_run_events (
    run_id TEXT NOT NULL REFERENCES wake_dispatches(run_id),
    event_id TEXT NOT NULL REFERENCES wake_events(id),
    PRIMARY KEY (run_id, event_id)
);

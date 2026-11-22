CREATE TABLE run_activity (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    identity_hash TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    heartbeat_at INTEGER NOT NULL,
    clock_invalid INTEGER NOT NULL DEFAULT 0 CHECK (clock_invalid IN (0, 1))
);

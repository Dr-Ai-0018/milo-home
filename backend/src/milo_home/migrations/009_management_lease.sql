CREATE TABLE management_lease (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    identity_hash TEXT NOT NULL CHECK (length(identity_hash) = 64),
    boot_id TEXT NOT NULL CHECK (length(boot_id) > 0),
    policy_hash TEXT NOT NULL CHECK (length(policy_hash) = 64),
    started_at INTEGER NOT NULL CHECK (started_at >= 0),
    heartbeat_at INTEGER NOT NULL CHECK (heartbeat_at >= started_at),
    ended_at INTEGER CHECK (ended_at >= heartbeat_at),
    invalid_reason TEXT CHECK (invalid_reason IN ('clock_invalid', 'lease_expired'))
);

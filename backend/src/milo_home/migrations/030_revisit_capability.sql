CREATE TABLE reply_revisit_capabilities (
    owner_id TEXT PRIMARY KEY REFERENCES users(id),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    updated_at INTEGER NOT NULL
);

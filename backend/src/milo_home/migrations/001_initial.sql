CREATE TABLE users (
    id TEXT PRIMARY KEY,
    handle TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE invitations (
    token_hash TEXT PRIMARY KEY,
    handle TEXT NOT NULL UNIQUE,
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX sessions_expiry ON sessions(expires_at);

CREATE TABLE auth_buckets (
    key_hash TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    attempts INTEGER NOT NULL
);

CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL UNIQUE REFERENCES users(id),
    created_at INTEGER NOT NULL,
    UNIQUE (id, owner_id)
);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'stopping', 'succeeded', 'failed', 'cancelled', 'timed_out'
    )),
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    ended_at INTEGER,
    failure_code TEXT,
    UNIQUE (id, thread_id)
);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id),
    sequence INTEGER NOT NULL,
    author_id TEXT REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('visitor', 'milo')),
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
    client_key TEXT,
    in_reply_to TEXT UNIQUE,
    run_id TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (id, thread_id),
    UNIQUE (thread_id, sequence),
    UNIQUE (author_id, client_key),
    FOREIGN KEY (in_reply_to, thread_id) REFERENCES messages(id, thread_id),
    FOREIGN KEY (run_id, thread_id) REFERENCES runs(id, thread_id),
    FOREIGN KEY (thread_id, author_id) REFERENCES threads(id, owner_id),
    CHECK (
        (role = 'visitor' AND author_id IS NOT NULL AND client_key IS NOT NULL
            AND in_reply_to IS NULL AND run_id IS NULL)
        OR
        (role = 'milo' AND author_id IS NULL AND client_key IS NULL
            AND in_reply_to IS NOT NULL AND run_id IS NOT NULL)
    )
);

CREATE TABLE run_messages (
    run_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    PRIMARY KEY (run_id, message_id),
    FOREIGN KEY (run_id, thread_id) REFERENCES runs(id, thread_id),
    FOREIGN KEY (message_id, thread_id) REFERENCES messages(id, thread_id)
);

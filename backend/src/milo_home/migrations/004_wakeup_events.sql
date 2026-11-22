CREATE TABLE wake_events (
    id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('message', 'fallback')),
    thread_id TEXT NOT NULL REFERENCES threads(id),
    message_id TEXT UNIQUE,
    scheduled_at INTEGER,
    observed_at INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'expired')),
    FOREIGN KEY (message_id, thread_id) REFERENCES messages(id, thread_id),
    CHECK (
        (kind = 'message' AND message_id IS NOT NULL AND scheduled_at IS NULL)
        OR
        (kind = 'fallback' AND message_id IS NULL AND scheduled_at IS NOT NULL)
    )
);
CREATE INDEX wake_events_pending ON wake_events(thread_id, kind, state);

CREATE TABLE wake_event_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_scan_at INTEGER,
    schedule_revision INTEGER
);
INSERT INTO wake_event_cursor (id) VALUES (1);

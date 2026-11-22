CREATE UNIQUE INDEX conversations_scope_identity ON conversations(id, thread_id);

CREATE TABLE knocks (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    client_key TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('accepted', 'coalesced')),
    received_at INTEGER NOT NULL,
    next_allowed_at INTEGER NOT NULL,
    response_phase TEXT NOT NULL CHECK (response_phase IN (
        'working', 'winding_down', 'resting', 'paused', 'unknown'
    )),
    UNIQUE (owner_id, client_key),
    UNIQUE (id, owner_id),
    FOREIGN KEY (thread_id, owner_id) REFERENCES threads(id, owner_id),
    FOREIGN KEY (conversation_id, thread_id) REFERENCES conversations(id, thread_id),
    FOREIGN KEY (signal_id, owner_id) REFERENCES knocks(id, owner_id),
    CHECK (
        (disposition = 'accepted' AND signal_id = id AND next_allowed_at = received_at + 30)
        OR (disposition = 'coalesced' AND signal_id != id AND received_at < next_allowed_at)
    )
);
CREATE INDEX knocks_owner_signals ON knocks(owner_id, received_at DESC)
    WHERE disposition = 'accepted';

CREATE TRIGGER knocks_signal_insert BEFORE INSERT ON knocks
WHEN NEW.disposition = 'coalesced' AND NOT EXISTS (
    SELECT 1 FROM knocks WHERE id = NEW.signal_id AND owner_id = NEW.owner_id
        AND disposition = 'accepted' AND next_allowed_at = NEW.next_allowed_at
)
BEGIN
    SELECT RAISE(ABORT, 'Invalid knock signal');
END;

CREATE TABLE run_knocks (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    knock_id TEXT NOT NULL REFERENCES knocks(id)
);
CREATE INDEX run_knocks_signal ON run_knocks(knock_id);
CREATE INDEX knocks_conversation_signals ON knocks(conversation_id, received_at DESC)
    WHERE disposition = 'accepted';

CREATE TRIGGER run_knocks_scope_insert BEFORE INSERT ON run_knocks
WHEN NOT EXISTS (
    SELECT 1 FROM runs JOIN knocks ON knocks.id = NEW.knock_id
    WHERE runs.id = NEW.run_id AND runs.status = 'queued'
        AND knocks.disposition = 'accepted'
        AND knocks.conversation_id = runs.conversation_id
        AND knocks.received_at <= runs.created_at
)
BEGIN
    SELECT RAISE(ABORT, 'Invalid run knock scope');
END;

CREATE TRIGGER run_knocks_immutable BEFORE UPDATE ON run_knocks
BEGIN
    SELECT RAISE(ABORT, 'Run knock snapshot is immutable');
END;

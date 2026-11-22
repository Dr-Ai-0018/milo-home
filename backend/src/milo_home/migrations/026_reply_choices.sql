CREATE TABLE message_intake (
    message_id TEXT PRIMARY KEY REFERENCES messages(id)
);
CREATE TRIGGER message_intake_insert AFTER INSERT ON messages
WHEN NEW.role = 'visitor'
BEGIN
    INSERT INTO message_intake VALUES (NEW.id);
END;

CREATE TABLE revisit_intents (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    origin_run_id TEXT NOT NULL REFERENCES runs(id),
    origin_index INTEGER NOT NULL CHECK (origin_index >= 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    state TEXT NOT NULL CHECK (state IN (
        'scheduled', 'running', 'blocked', 'needs_reconciliation', 'completed', 'cancelled'
    )),
    due_at INTEGER NOT NULL,
    initial_due_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    blocked_reason TEXT CHECK (blocked_reason IN (
        'schedule', 'reception', 'budget', 'service', 'occupancy'
    )),
    CHECK ((state = 'blocked') = (blocked_reason IS NOT NULL)),
    CHECK (initial_due_at > created_at),
    UNIQUE (origin_run_id, origin_index)
);
CREATE INDEX revisit_intents_due ON revisit_intents (state, due_at, id);
CREATE INDEX revisit_intents_conversation ON revisit_intents (conversation_id, state, due_at);
CREATE TRIGGER revisit_intents_scope_insert BEFORE INSERT ON revisit_intents
BEGIN
    SELECT RAISE(ABORT, 'Revisit origin scope mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM runs JOIN threads ON threads.id = runs.thread_id
        WHERE runs.id = NEW.origin_run_id AND threads.owner_id = NEW.owner_id
            AND runs.conversation_id = NEW.conversation_id
    );
END;
CREATE TRIGGER revisit_intents_identity_immutable
BEFORE UPDATE OF id, owner_id, conversation_id, origin_run_id, origin_index, created_at, initial_due_at
ON revisit_intents
BEGIN
    SELECT RAISE(ABORT, 'Revisit identity is immutable');
END;

CREATE TABLE revisit_sources (
    revisit_id TEXT NOT NULL REFERENCES revisit_intents(id),
    message_id TEXT NOT NULL REFERENCES messages(id),
    PRIMARY KEY (revisit_id, message_id)
);
CREATE TRIGGER revisit_sources_scope_insert BEFORE INSERT ON revisit_sources
BEGIN
    SELECT RAISE(ABORT, 'Revisit source scope mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM revisit_intents JOIN messages
        ON messages.conversation_id = revisit_intents.conversation_id
        WHERE revisit_intents.id = NEW.revisit_id AND messages.id = NEW.message_id
            AND messages.role = 'visitor' AND messages.author_id = revisit_intents.owner_id
    );
END;
CREATE TRIGGER revisit_sources_immutable BEFORE UPDATE ON revisit_sources
BEGIN
    SELECT RAISE(ABORT, 'Revisit source is immutable');
END;

CREATE TABLE message_reply_links (
    reply_id TEXT NOT NULL REFERENCES messages(id),
    message_id TEXT NOT NULL REFERENCES messages(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    PRIMARY KEY (reply_id, message_id),
    UNIQUE (run_id, message_id)
);
CREATE INDEX message_reply_links_input ON message_reply_links (message_id, reply_id);
CREATE TRIGGER message_reply_links_scope_insert BEFORE INSERT ON message_reply_links
BEGIN
    SELECT RAISE(ABORT, 'Reply coverage scope mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM messages AS incoming JOIN messages AS reply
        ON reply.conversation_id = incoming.conversation_id
        JOIN runs ON runs.id = reply.run_id
        WHERE incoming.id = NEW.message_id AND incoming.role = 'visitor'
            AND reply.id = NEW.reply_id AND reply.role = 'milo' AND runs.id = NEW.run_id
            AND runs.conversation_id = incoming.conversation_id
    );
END;
CREATE TRIGGER message_reply_links_immutable BEFORE UPDATE ON message_reply_links
BEGIN
    SELECT RAISE(ABORT, 'Reply coverage is immutable');
END;

CREATE TABLE message_decisions (
    message_id TEXT NOT NULL REFERENCES messages(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    run_id TEXT NOT NULL REFERENCES runs(id),
    state TEXT NOT NULL CHECK (state IN ('replied', 'no_reply', 'deferred')),
    decided_at INTEGER NOT NULL,
    reply_id TEXT REFERENCES messages(id),
    revisit_id TEXT REFERENCES revisit_intents(id),
    CHECK ((state = 'replied') = (reply_id IS NOT NULL)),
    CHECK (state != 'deferred' OR revisit_id IS NOT NULL),
    PRIMARY KEY (message_id, revision),
    UNIQUE (run_id, message_id)
);
CREATE INDEX message_decisions_run ON message_decisions (run_id, message_id);
CREATE TRIGGER message_decisions_scope_insert BEFORE INSERT ON message_decisions
BEGIN
    SELECT RAISE(ABORT, 'Decision scope mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM messages JOIN runs ON runs.conversation_id = messages.conversation_id
        WHERE messages.id = NEW.message_id AND messages.role = 'visitor' AND runs.id = NEW.run_id
    );
    SELECT RAISE(ABORT, 'Decision revision mismatch') WHERE NEW.revision != (
        SELECT coalesce(max(revision), 0) + 1 FROM message_decisions
        WHERE message_id = NEW.message_id
    );
    SELECT RAISE(ABORT, 'Decision reply mismatch') WHERE NEW.reply_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM message_reply_links WHERE reply_id = NEW.reply_id
            AND message_id = NEW.message_id AND run_id = NEW.run_id
    );
    SELECT RAISE(ABORT, 'Decision revisit mismatch') WHERE NEW.revisit_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM revisit_sources JOIN revisit_intents ON revisit_intents.id = revisit_sources.revisit_id
        WHERE revisit_sources.revisit_id = NEW.revisit_id
            AND revisit_sources.message_id = NEW.message_id
            AND revisit_intents.origin_run_id = NEW.run_id
    );
END;
CREATE TRIGGER message_decisions_immutable BEFORE UPDATE ON message_decisions
BEGIN
    SELECT RAISE(ABORT, 'Decision history is immutable');
END;

CREATE VIEW message_reply_state AS
SELECT incoming.id AS message_id, incoming.thread_id, incoming.conversation_id,
    incoming.sequence, incoming.created_at, incoming.rowid AS source_rowid,
    coalesce(decision.state, CASE WHEN legacy.id IS NOT NULL THEN 'replied'
        WHEN intake.message_id IS NOT NULL THEN 'unprocessed' ELSE 'unknown' END) AS state,
    decision.revision, decision.decided_at, decision.revisit_id,
    CASE WHEN decision.message_id IS NOT NULL THEN decision.reply_id ELSE legacy.id END AS reply_id
FROM messages AS incoming
LEFT JOIN message_intake AS intake ON intake.message_id = incoming.id
LEFT JOIN message_decisions AS decision ON decision.message_id = incoming.id
    AND decision.revision = (SELECT max(revision) FROM message_decisions WHERE message_id = incoming.id)
LEFT JOIN messages AS legacy ON legacy.in_reply_to = incoming.id
WHERE incoming.role = 'visitor';

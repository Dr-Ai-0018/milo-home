CREATE TABLE revisit_claims (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    revisit_id TEXT NOT NULL REFERENCES revisit_intents(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    due_at INTEGER NOT NULL,
    claimed_at INTEGER NOT NULL,
    UNIQUE (revisit_id, revision)
);
CREATE TRIGGER revisit_claims_scope BEFORE INSERT ON revisit_claims
BEGIN
    SELECT RAISE(ABORT, 'Revisit claim scope mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM revisit_intents v JOIN runs r ON r.conversation_id = v.conversation_id
        JOIN threads t ON t.id = r.thread_id
        WHERE r.id = NEW.run_id AND v.id = NEW.revisit_id AND v.owner_id = t.owner_id
            AND v.revision = NEW.revision AND v.state IN ('scheduled', 'blocked')
            AND v.due_at = NEW.due_at AND v.due_at <= NEW.claimed_at
    );
END;
CREATE TRIGGER revisit_claims_immutable BEFORE UPDATE ON revisit_claims
BEGIN
    SELECT RAISE(ABORT, 'Revisit claim birth evidence is immutable');
END;

CREATE TABLE revisit_claim_sources (
    run_id TEXT NOT NULL REFERENCES revisit_claims(run_id),
    message_id TEXT NOT NULL REFERENCES messages(id),
    decision_revision INTEGER NOT NULL CHECK (decision_revision >= 1),
    decision_state TEXT NOT NULL CHECK (decision_state IN ('replied', 'no_reply', 'deferred')),
    PRIMARY KEY (run_id, message_id)
);
CREATE TRIGGER revisit_claim_sources_scope BEFORE INSERT ON revisit_claim_sources
BEGIN
    SELECT RAISE(ABORT, 'Revisit claim source mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM revisit_claims c JOIN revisit_sources s ON s.revisit_id = c.revisit_id
        JOIN message_reply_state m ON m.message_id = s.message_id
        WHERE c.run_id = NEW.run_id AND s.message_id = NEW.message_id
            AND m.revision = NEW.decision_revision AND m.state = NEW.decision_state
    );
END;
CREATE TRIGGER revisit_claim_sources_immutable BEFORE UPDATE ON revisit_claim_sources
BEGIN
    SELECT RAISE(ABORT, 'Revisit claim source is immutable');
END;

CREATE TABLE revisit_controls (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(id),
    revisit_id TEXT NOT NULL REFERENCES revisit_intents(id),
    expected_revision INTEGER NOT NULL CHECK (expected_revision >= 1),
    revision INTEGER NOT NULL CHECK (revision = expected_revision + 1),
    action TEXT NOT NULL CHECK (action IN ('reschedule', 'cancel')),
    due_at INTEGER,
    created_at INTEGER NOT NULL,
    CHECK ((action = 'reschedule' AND due_at > created_at) OR
        (action = 'cancel' AND due_at IS NULL)),
    UNIQUE (revisit_id, revision)
);
CREATE TRIGGER revisit_controls_scope BEFORE INSERT ON revisit_controls
BEGIN
    SELECT RAISE(ABORT, 'Revisit control owner mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM revisit_intents WHERE id = NEW.revisit_id AND owner_id = NEW.owner_id
    );
END;
CREATE TRIGGER revisit_controls_immutable BEFORE UPDATE ON revisit_controls
BEGIN
    SELECT RAISE(ABORT, 'Revisit control is immutable');
END;

CREATE TABLE message_control_decisions (
    message_id TEXT NOT NULL REFERENCES messages(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    control_id TEXT NOT NULL REFERENCES revisit_controls(id),
    state TEXT NOT NULL CHECK (state = 'no_reply'),
    decided_at INTEGER NOT NULL,
    PRIMARY KEY (message_id, revision),
    UNIQUE (control_id, message_id)
);
CREATE TRIGGER message_control_decisions_immutable BEFORE UPDATE ON message_control_decisions
BEGIN
    SELECT RAISE(ABORT, 'Controlled decision is immutable');
END;

CREATE VIEW message_decision_history AS
SELECT message_id, revision, run_id, state, decided_at, reply_id, revisit_id FROM message_decisions
UNION ALL
SELECT message_id, revision, NULL, state, decided_at, NULL, NULL FROM message_control_decisions;

CREATE TRIGGER message_control_decisions_scope BEFORE INSERT ON message_control_decisions
BEGIN
    SELECT RAISE(ABORT, 'Controlled decision revision mismatch') WHERE NEW.revision != (
        SELECT coalesce(max(revision), 0) + 1 FROM message_decision_history
        WHERE message_id = NEW.message_id
    );
    SELECT RAISE(ABORT, 'Controlled decision source mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM revisit_controls c JOIN messages m ON m.author_id = c.owner_id
        JOIN message_reply_state d ON d.message_id = m.id
        WHERE c.id = NEW.control_id AND c.action = 'cancel' AND m.id = NEW.message_id
            AND d.state = 'deferred' AND d.revisit_id = c.revisit_id
            AND c.created_at = NEW.decided_at
    );
END;

DROP VIEW message_reply_state;
CREATE VIEW message_reply_state AS
SELECT incoming.id AS message_id, incoming.thread_id, incoming.conversation_id,
    incoming.sequence, incoming.created_at, incoming.rowid AS source_rowid,
    coalesce(decision.state, CASE WHEN legacy.id IS NOT NULL THEN 'replied'
        WHEN intake.message_id IS NOT NULL THEN 'unprocessed' ELSE 'unknown' END) AS state,
    decision.revision, decision.decided_at, decision.revisit_id,
    CASE WHEN decision.message_id IS NOT NULL THEN decision.reply_id ELSE legacy.id END AS reply_id
FROM messages AS incoming
LEFT JOIN message_intake AS intake ON intake.message_id = incoming.id
LEFT JOIN message_decision_history AS decision ON decision.message_id = incoming.id
    AND decision.revision = (SELECT max(revision) FROM message_decision_history WHERE message_id = incoming.id)
LEFT JOIN messages AS legacy ON legacy.in_reply_to = incoming.id
WHERE incoming.role = 'visitor';

DROP TRIGGER message_decisions_scope_insert;
CREATE TRIGGER message_decisions_scope_insert BEFORE INSERT ON message_decisions
BEGIN
    SELECT RAISE(ABORT, 'Decision scope mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM messages JOIN runs ON runs.conversation_id = messages.conversation_id
        WHERE messages.id = NEW.message_id AND messages.role = 'visitor' AND runs.id = NEW.run_id
    );
    SELECT RAISE(ABORT, 'Decision revision mismatch') WHERE NEW.revision != (
        SELECT coalesce(max(revision), 0) + 1 FROM message_decision_history
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

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id),
    kind TEXT NOT NULL CHECK (kind IN ('letter', 'chat')),
    title TEXT CHECK (title IS NULL OR length(title) BETWEEN 1 AND 80),
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    client_key TEXT,
    created_at INTEGER NOT NULL,
    CHECK (is_default = 0 OR kind = 'letter'),
    CHECK (kind = 'letter' OR title IS NULL),
    UNIQUE (thread_id, client_key)
);
CREATE UNIQUE INDEX conversations_default ON conversations(thread_id) WHERE is_default = 1;
CREATE UNIQUE INDEX conversations_chat ON conversations(thread_id) WHERE kind = 'chat';
CREATE INDEX conversations_thread ON conversations(thread_id, created_at, id);

INSERT INTO conversations (id, thread_id, kind, is_default, created_at)
    SELECT id, id, 'letter', 1, created_at FROM threads;

ALTER TABLE messages ADD COLUMN conversation_id TEXT REFERENCES conversations(id);
ALTER TABLE runs ADD COLUMN conversation_id TEXT REFERENCES conversations(id);
UPDATE messages SET conversation_id = thread_id;
UPDATE runs SET conversation_id = thread_id;
CREATE INDEX messages_conversation ON messages(conversation_id, sequence);

CREATE TRIGGER threads_default_conversation AFTER INSERT ON threads
BEGIN
    INSERT INTO conversations (id, thread_id, kind, is_default, created_at)
        VALUES (NEW.id, NEW.id, 'letter', 1, NEW.created_at);
END;

CREATE TRIGGER conversations_identity_immutable
BEFORE UPDATE OF id, thread_id, kind, is_default, client_key ON conversations
WHEN NEW.id IS NOT OLD.id OR NEW.thread_id IS NOT OLD.thread_id
    OR NEW.kind IS NOT OLD.kind OR NEW.is_default IS NOT OLD.is_default
    OR NEW.client_key IS NOT OLD.client_key
BEGIN
    SELECT RAISE(ABORT, 'Conversation identity is immutable');
END;

CREATE TRIGGER messages_conversation_insert BEFORE INSERT ON messages
WHEN NEW.conversation_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'Message conversation mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM conversations WHERE id = NEW.conversation_id AND thread_id = NEW.thread_id
    );
    SELECT RAISE(ABORT, 'Reply conversation mismatch') WHERE NEW.role = 'milo' AND NOT EXISTS (
        SELECT 1 FROM messages AS original JOIN runs ON runs.id = NEW.run_id
        WHERE original.id = NEW.in_reply_to AND original.conversation_id = NEW.conversation_id
            AND runs.conversation_id = NEW.conversation_id
    );
END;

CREATE TRIGGER messages_conversation_update
BEFORE UPDATE OF conversation_id, thread_id, in_reply_to, run_id, role ON messages
BEGIN
    SELECT RAISE(ABORT, 'Message conversation is immutable')
        WHERE OLD.conversation_id IS NOT NULL AND NEW.conversation_id IS NOT OLD.conversation_id;
    SELECT RAISE(ABORT, 'Message conversation mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM conversations WHERE id = NEW.conversation_id AND thread_id = NEW.thread_id
    );
    SELECT RAISE(ABORT, 'Reply conversation mismatch') WHERE NEW.role = 'milo' AND NOT EXISTS (
        SELECT 1 FROM messages AS original JOIN runs ON runs.id = NEW.run_id
        WHERE original.id = NEW.in_reply_to AND original.conversation_id = NEW.conversation_id
            AND runs.conversation_id = NEW.conversation_id
    );
END;

CREATE TRIGGER messages_default_conversation AFTER INSERT ON messages
WHEN NEW.conversation_id IS NULL
BEGIN
    UPDATE messages SET conversation_id = CASE WHEN NEW.role = 'milo'
        THEN (SELECT conversation_id FROM messages WHERE id = NEW.in_reply_to)
        ELSE (SELECT id FROM conversations WHERE thread_id = NEW.thread_id AND is_default = 1)
        END WHERE id = NEW.id;
END;

CREATE TRIGGER runs_conversation_insert BEFORE INSERT ON runs
WHEN NEW.conversation_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'Run conversation mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM conversations WHERE id = NEW.conversation_id AND thread_id = NEW.thread_id
    );
END;

CREATE TRIGGER runs_conversation_update BEFORE UPDATE OF conversation_id, thread_id ON runs
BEGIN
    SELECT RAISE(ABORT, 'Run conversation is immutable')
        WHERE OLD.conversation_id IS NOT NULL AND NEW.conversation_id IS NOT OLD.conversation_id;
    SELECT RAISE(ABORT, 'Run conversation mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM conversations WHERE id = NEW.conversation_id AND thread_id = NEW.thread_id
    );
END;

CREATE TRIGGER runs_default_conversation AFTER INSERT ON runs
WHEN NEW.conversation_id IS NULL
BEGIN
    UPDATE runs SET conversation_id = (
        SELECT id FROM conversations WHERE thread_id = NEW.thread_id AND is_default = 1
    ) WHERE id = NEW.id;
END;

CREATE TRIGGER run_messages_conversation_insert BEFORE INSERT ON run_messages
BEGIN
    SELECT RAISE(ABORT, 'Run selection conversation mismatch') WHERE NOT EXISTS (
        SELECT 1 FROM messages JOIN runs ON runs.id = NEW.run_id
        WHERE messages.id = NEW.message_id AND messages.conversation_id = runs.conversation_id
    );
END;

CREATE TRIGGER run_messages_conversation_update BEFORE UPDATE ON run_messages
BEGIN
    SELECT RAISE(ABORT, 'Run selection is immutable');
END;

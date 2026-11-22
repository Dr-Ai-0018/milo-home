CREATE TABLE work_access (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX work_single_operator ON work_access((1)) WHERE enabled = 1;

CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    main_slot INTEGER NOT NULL UNIQUE CHECK (main_slot = 1),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    root_path TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    updated_at INTEGER NOT NULL
);

CREATE TABLE work_sessions (
    id TEXT PRIMARY KEY,
    main_slot INTEGER NOT NULL UNIQUE CHECK (main_slot = 1),
    owner_id TEXT NOT NULL REFERENCES work_access(user_id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    created_at INTEGER NOT NULL,
    UNIQUE (id, owner_id, workspace_id)
);
CREATE TRIGGER work_session_identity_immutable
BEFORE UPDATE ON work_sessions
BEGIN
    SELECT RAISE(ABORT, 'work session identity is immutable');
END;

CREATE TABLE work_tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    owner_revision INTEGER NOT NULL CHECK (owner_revision >= 1),
    workspace_revision INTEGER NOT NULL CHECK (workspace_revision >= 1),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'stopping', 'completed', 'failed', 'cancelled',
        'timed_out', 'needs_reconciliation'
    )),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    stop_requested_at INTEGER,
    UNIQUE (session_id, sequence),
    UNIQUE (id, owner_id),
    FOREIGN KEY (session_id, owner_id, workspace_id)
        REFERENCES work_sessions(id, owner_id, workspace_id)
);
CREATE INDEX work_tasks_owner_status ON work_tasks(owner_id, status);
CREATE TRIGGER work_task_identity_immutable
BEFORE UPDATE OF id, session_id, owner_id, workspace_id, owner_revision,
    workspace_revision, sequence, created_at ON work_tasks
BEGIN
    SELECT RAISE(ABORT, 'work task identity is immutable');
END;

CREATE TABLE work_inputs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    kind TEXT NOT NULL CHECK (kind IN ('initial', 'supplement')),
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
    client_key TEXT NOT NULL,
    delivery TEXT NOT NULL CHECK (delivery IN (
        'queued', 'sending', 'acknowledged', 'unknown', 'cancelled', 'conflict'
    )),
    created_at INTEGER NOT NULL,
    UNIQUE (owner_id, client_key),
    UNIQUE (task_id, sequence),
    FOREIGN KEY (task_id, owner_id) REFERENCES work_tasks(id, owner_id),
    CHECK ((kind = 'initial' AND sequence = 1) OR (kind = 'supplement' AND sequence > 1))
);
CREATE TRIGGER work_input_identity_immutable
BEFORE UPDATE OF id, task_id, owner_id, sequence, kind, body, client_key, created_at ON work_inputs
BEGIN
    SELECT RAISE(ABORT, 'work input identity is immutable');
END;

CREATE TABLE work_stops (
    owner_id TEXT NOT NULL,
    client_key TEXT NOT NULL,
    task_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (owner_id, client_key),
    FOREIGN KEY (task_id, owner_id) REFERENCES work_tasks(id, owner_id)
);

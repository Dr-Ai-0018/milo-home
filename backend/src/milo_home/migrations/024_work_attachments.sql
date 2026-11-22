CREATE TABLE work_attachment_batches (
    task_id TEXT PRIMARY KEY NOT NULL REFERENCES work_receipt_tasks(task_id),
    state TEXT NOT NULL CHECK (state IN ('pending', 'none', 'available', 'unavailable')),
    sealed_at INTEGER NOT NULL CHECK (sealed_at >= 0)
);

CREATE TABLE work_attachments (
    id TEXT PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL REFERENCES work_attachment_batches(task_id),
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    UNIQUE (task_id, filename)
);

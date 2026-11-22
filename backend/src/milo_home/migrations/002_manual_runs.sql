ALTER TABLE runs ADD COLUMN deadline_at INTEGER;
ALTER TABLE runs ADD COLUMN unit_name TEXT;
ALTER TABLE runs ADD COLUMN stop_reason TEXT;
CREATE UNIQUE INDEX runs_single_active ON runs ((1))
    WHERE status IN ('queued', 'running', 'stopping');
CREATE INDEX runs_thread_time ON runs (thread_id, created_at DESC);

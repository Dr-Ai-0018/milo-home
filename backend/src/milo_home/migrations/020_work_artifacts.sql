-- Explicitly checked text snapshots. Existing tasks start with no evidence.
CREATE TABLE work_artifacts (
    task_id TEXT NOT NULL REFERENCES work_tasks(id),
    path TEXT NOT NULL CHECK (length(path) BETWEEN 1 AND 240),
    text TEXT NOT NULL CHECK (length(CAST(text AS BLOB)) <= 16384),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    checked_at INTEGER NOT NULL CHECK (checked_at >= 0),
    PRIMARY KEY (task_id, path)
);

CREATE TRIGGER work_artifact_no_update BEFORE UPDATE ON work_artifacts
BEGIN
    SELECT RAISE(ABORT, 'work artifact cannot be replaced');
END;

CREATE TABLE work_deliverables (
    task_id TEXT PRIMARY KEY REFERENCES work_tasks(id),
    filename TEXT NOT NULL CHECK (length(filename) BETWEEN 5 AND 100),
    observation TEXT CHECK (observation IN ('saved', 'missing', 'withheld'))
);

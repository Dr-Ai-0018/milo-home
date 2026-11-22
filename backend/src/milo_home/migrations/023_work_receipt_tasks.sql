CREATE TABLE work_receipt_tasks (
    task_id TEXT PRIMARY KEY NOT NULL REFERENCES work_tasks(id)
);

CREATE TRIGGER work_receipt_task_created AFTER INSERT ON work_tasks
BEGIN
    INSERT INTO work_receipt_tasks (task_id) VALUES (NEW.id);
END;

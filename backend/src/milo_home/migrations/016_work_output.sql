-- A sealed output is published atomically with its immutable result/event.
-- NULL preserves old receipts without inventing a body or zero tool counts.
ALTER TABLE work_results ADD COLUMN output TEXT
    CHECK (output IS NULL OR (json_valid(output) AND length(output) <= 60000));

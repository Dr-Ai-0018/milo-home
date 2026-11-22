"""Authorized facts and sealed answers; never raw private protocol."""

import json

from .errors import ApiError
from .work_access import require_task
from .work_output_models import WorkOutputView, missing_output

RESULT_FIELDS = ("id", "task_id", "input_id", "outcome", "observed_at")
EVENT_FIELDS = ("task_id", "sequence", "kind", "state", "input_id", "result_id", "recorded_at")


def page_arguments(after, limit):
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Invalid work progress page")


class WorkProgress:
    def __init__(self, database):
        self.database = database

    def results(self, owner, task_id):
        # A task has at most 100 inputs, each with at most one launch permit.
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            require_task(connection, owner, task_id)
            rows = connection.execute(
                "SELECT r.* FROM work_results r JOIN work_inputs i ON i.id=r.input_id "
                "WHERE r.task_id=? ORDER BY i.sequence",
                (task_id,),
            ).fetchall()
            results = []
            for row in rows:
                output = json.loads(row["output"]) if row["output"] else missing_output()
                results.append(
                    {
                        **{key: row[key] for key in RESULT_FIELDS},
                        "output": WorkOutputView.model_validate(output).model_dump(),
                    }
                )
            return {"results": results}

    def events(self, owner, task_id, after=0, limit=20):
        page_arguments(after, limit)
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            require_task(connection, owner, task_id)
            high = connection.execute(
                "SELECT coalesce(max(sequence),0) FROM work_events WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            if after > high:
                raise ApiError(409, "work_cursor_ahead", "记录游标已变化，请重新读取任务。")
            rows = connection.execute(
                "SELECT * FROM work_events WHERE task_id=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (task_id, after, limit),
            ).fetchall()
            next_after = rows[-1]["sequence"] if rows else after
            return {
                "events": [{key: row[key] for key in EVENT_FIELDS} for row in rows],
                "next_after": next_after,
                "has_more": next_after < high,
            }

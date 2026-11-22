from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_work_api import http_case as http_case
from test_work_api import initialize
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case

from milo_home.work_artifacts import list_artifacts
from milo_home.work_progress import WorkProgress


def test_new_task_names_one_file_and_retries_are_bound_to_it(http_case):
    client, _, _, _, tokens = http_case
    session = initialize(http_case)
    base = f"/api/work-sessions/{session['id']}/tasks"
    headers = {"Idempotency-Key": str(uuid4())}
    body = {"body": "写一张纸条", "deliverable_name": "小纸条.txt"}
    for invalid in [
        "../private.txt",
        "/root/private.txt",
        ".hidden.txt",
        "a.pdf",
        "字" * 33 + ".txt",
    ]:
        assert (
            client.post(
                base, headers=headers, json={**body, "deliverable_name": invalid}
            ).status_code
            == 422
        )
    created = client.post(base, headers=headers, json=body)
    assert created.status_code == 202
    task = created.json()
    assert task["deliverable"] == {
        "filename": "小纸条.txt",
        "path": f"deliverables/{task['id']}/小纸条.txt",
        "state": "pending",
    }
    assert client.post(base, headers=headers, json=body).status_code == 200
    assert (
        client.post(base, headers=headers, json={**body, "deliverable_name": None}).status_code
        == 409
    )
    assert client.get(f"/api/work-tasks/{task['id']}").json() == task
    client.cookies.set("milo_session", tokens[1])
    assert client.get(f"/api/work-tasks/{task['id']}").status_code == 404


@pytest.mark.parametrize(
    ("scenario", "state"),
    [
        ("deliver-file", "saved"),
        ("normal", "missing"),
        ("deliver-sensitive", "withheld"),
        ("deliver-link", "withheld"),
        ("failed-terminal", "pending"),
    ],
)
def test_real_launcher_collects_only_the_agreed_file_after_completion(
    launch_case, fake_units, tmp_path, scenario, state
):
    case, _, _, launcher, _, selected = launch_case
    work, control, *_ = case
    database, owner, _, _, _, tasks, session = work
    selected[0] = scenario
    task, _ = tasks.create(owner, session["id"], "交回纸条", str(uuid4()), "note.txt")
    task_id = task["id"]
    # An existing similarly named workspace file must never be collected.
    (tmp_path / "note.txt").write_text("unrelated", encoding="utf-8")
    if scenario == "deliver-file":
        launcher.run_task(owner, task_id, 60, credential="synthetic-secret")
    else:
        window = control.reserve(owner, task_id, 60)
        launcher.run(owner, window["id"], credential="synthetic-secret")
    latest = tasks.get(owner, task_id)
    assert latest["deliverable"]["state"] == state
    assert WorkProgress(database).results(owner, task_id)["results"]
    saved = list_artifacts(database, owner, task_id)["artifacts"]
    if state == "saved":
        assert len(saved) == 1 and saved[0]["text"] == "实际交回的内容。\n"
        path = tmp_path / latest["deliverable"]["path"]
        path.write_text("later", encoding="utf-8")
        assert list_artifacts(database, owner, task_id)["artifacts"] == saved
    else:
        assert saved == []

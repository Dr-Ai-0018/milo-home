from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case

from milo_home.work_artifacts import list_artifacts
from milo_home.work_attachment_batches import read_attachments
from milo_home.work_attachment_store import AttachmentStore
from milo_home.work_task_receipt import read_receipt


@pytest.mark.parametrize(
    ("scenario", "expected"), [("attachment", "available"), ("attachment-secret", "unavailable")]
)
def test_work_collection_keeps_task_receipt_and_legacy_txt_independent(
    launch_case, fake_units, private_runtime_directory, scenario, expected
):
    case, _, _, launcher, _, selected = launch_case
    database, owner, _, _, _, tasks, session = case[0]
    root = private_runtime_directory("milo-attachments-")
    launcher.attachment_store = AttachmentStore(root, max_bytes=1024)
    selected[0] = scenario
    task, _ = tasks.create(owner, session["id"], "交回文件", str(uuid4()), "note.txt")
    task_id = task["id"]
    _, result = launcher.run_task(owner, task_id, 60, credential="synthetic-secret")
    assert result.outcome == "completed"
    receipt = read_receipt(database, owner, task_id)
    assert receipt["state"] == "available" and receipt["text"].endswith("合成结果")
    assert list_artifacts(database, owner, task_id)["artifacts"][0]["text"] == "实际交回的内容。\n"
    batch = read_attachments(database, owner, task_id)
    assert batch["state"] == expected
    if expected == "available":
        (item,) = batch["attachments"]
        assert launcher.attachment_store.read(task_id, item) == b"\x00\xff"
    else:
        assert batch["attachments"] == []

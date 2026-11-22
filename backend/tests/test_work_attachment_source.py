import os
from uuid import uuid4

import pytest

from milo_home import work_attachment_source as source


@pytest.fixture
def handoff(tmp_path):
    task_id = str(uuid4())
    path = tmp_path / "tasks" / task_id / "handoff"
    path.mkdir(parents=True)
    return tmp_path, task_id, path


def test_handoff_only_reads_explicit_files_without_extension_restriction(handoff):
    root, task_id, path = handoff
    (path / "图像.bin").write_bytes(b"\x00\xff")
    (path / "LICENSE").write_bytes(b"plain")
    work = path.parent / "work"
    work.mkdir()
    (work / "scratch.txt").write_bytes(b"not a deliverable")
    assert dict(source.handoff_files(root, task_id)) == {
        "图像.bin": b"\x00\xff",
        "LICENSE": b"plain",
    }


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "hidden", "slash"])
def test_handoff_rejects_entries_outside_regular_deliverables(handoff, kind):
    root, task_id, path = handoff
    outside = root / "private"
    outside.write_bytes(b"not a deliverable")
    item = path / "entry"
    if kind == "symlink":
        item.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, item)
    elif kind == "fifo":
        os.mkfifo(item)
    elif kind == "directory":
        item.mkdir()
    elif kind == "hidden":
        (path / ".hidden").touch()
    else:
        (path / "a／b").touch()
    with pytest.raises(ValueError):
        list(source.handoff_files(root, task_id))


def test_handoff_does_not_follow_task_directory_link(handoff):
    root, task_id, path = handoff
    path.rename(path.with_name("real"))
    path.symlink_to(path.with_name("real"), target_is_directory=True)
    with pytest.raises(OSError):
        list(source.handoff_files(root, task_id))


def test_handoff_bounds_count_and_total_bytes(handoff, monkeypatch):
    root, task_id, path = handoff
    (path / "a").write_bytes(b"abc")
    (path / "b").write_bytes(b"def")
    with monkeypatch.context() as limited:
        limited.setattr(source, "MAX_FILES", 1)
        with pytest.raises(ValueError, match="Too many"):
            list(source.handoff_files(root, task_id))
    monkeypatch.setattr(source, "MAX_TASK_BYTES", 5)
    with pytest.raises(ValueError, match="byte limit"):
        list(source.handoff_files(root, task_id))

import os
import shutil
import time

import pytest
from test_execution_control import execution_case as execution_case
from test_work_creation import call, requests, state
from test_work_creation import creation_case as creation_case
from test_work_initialization import init_case as init_case
from test_work_process import fake_units as fake_units
from test_work_tasks import work_case as work_case

from milo_home.codex_config import CodexRuntime
from milo_home.work_creation_records import CreationRecords
from milo_home.work_receiver import ReceiveLimit

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root creation")


@pytest.mark.parametrize("clock", ["wall", "monotonic", "rewind"])
@pytest.mark.parametrize("moment", ["preparation", "between_processes"])
def test_total_original_deadline_covers_preparation_and_both_processes(
    creation_case, monkeypatch, clock, moment
):
    wall, mono = [time.time()], [time.monotonic()]
    monkeypatch.setattr(
        "milo_home.work_creation.ReceiveLimit",
        lambda seconds, _: ReceiveLimit(seconds, lambda: wall[0], lambda: mono[0]),
    )

    def expire():
        if clock == "monotonic":
            mono[0] += 60
        else:
            wall[0] += -1 if clock == "rewind" else 60

    if moment == "preparation":
        monkeypatch.setattr(CodexRuntime, "verify", lambda _: expire())
    else:
        original = CreationRecords.record

        def record(self, event):
            original(self, event)
            if event.get("phase") == "stop_confirmed":
                expire()

        monkeypatch.setattr(CreationRecords, "record", record)
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    if moment == "preparation":
        assert not list(creation_case[0][1].iterdir()) and state(creation_case) == (None, False)
        assert not creation_case[1]
    else:
        assert state(creation_case) == ("unknown", True) and len(creation_case[1]) == 1
        assert not any(r["method"] == "thread/resume" for r in requests(creation_case))
    assert not list(creation_case[0][1].glob("*.env"))


@pytest.mark.parametrize("change", ["database_inode", "home", "config", "event_sequence"])
def test_recovery_refuses_changed_original_evidence_without_a_new_process(
    creation_case, monkeypatch, change, private_runtime_directory
):
    creation_case[3][0] = "resume-eof"
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    before = requests(creation_case)
    case = creation_case
    initial = case[0]
    if change == "database_inode":
        database = initial[0][0][0]
        copy = database.path.with_name("new-inode.sqlite3")
        database.backup(copy)
        os.replace(copy, database.path)
    elif change == "home":
        directory = private_runtime_directory("milo-recovery-copy-")
        shutil.copytree(initial[1], directory, dirs_exist_ok=True)
        case = ((initial[0], directory, *initial[2:]), *case[1:])
    elif change == "config":
        (initial[1] / "config.toml").write_bytes(b"changed")
    else:
        path = initial[1] / "main-creation-events" / "000001.json"
        path.rename(path.with_suffix(".preserved"))
    with pytest.raises(SystemExit):
        call(case, monkeypatch, recover=True)
    assert requests(creation_case) == before and len(creation_case[1]) == 2
    assert state(creation_case) == ("unknown", True)

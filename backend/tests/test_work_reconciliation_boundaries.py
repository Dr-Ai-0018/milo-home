import os
import sys
from pathlib import Path

import pytest
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_migration import remove_reconciliation_schema, snapshot
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_reconciliation import abandon
from test_work_reconciliation import fresh_stop as fresh_stop
from test_work_runtime import SYNTHETIC_CONFIG
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case

from milo_home.codex_config import CodexRuntime, NewawzConfig
from milo_home.database_identity import ServiceDatabase
from milo_home.execution_control import ExecutionControl
from milo_home.execution_lock import execution_lock
from milo_home.work_process import WorkLauncher
from milo_home.work_progress import WorkProgress
from milo_home.work_reconciliation import WorkReconciliation
from milo_home.work_tasks import WorkTasks

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root lease")


@pytest.mark.parametrize("repair", [False, True])
def test_reconciliation_keeps_private_evidence_root_owned_with_service_database(
    runtime_case, owned_database, fake_units, fresh_stop, monkeypatch, tmp_path, repair
):
    case, runtime, journal = runtime_case
    path, account = owned_database
    copied = path.parent / "seeded.sqlite3"
    case[0][0].backup(copied)
    os.chown(copied, account.pw_uid, account.pw_gid)
    os.replace(copied, path)
    runtime.database = ServiceDatabase(path, account.pw_name)
    # Reserve against the exact service-owned database before binding its inode.
    work_case = (
        runtime.database,
        *case[0][1:5],
        WorkTasks(runtime.database, lambda: 101),
        case[0][-1],
    )
    control = ExecutionControl(runtime.database, lambda: case[-1][0])
    service_case = (work_case, control, *case[2:])
    with execution_lock(runtime.database) as lease:
        runtime.bind(case[0][1], case[0][-1]["id"], lease=lease)
    installed = CodexRuntime(Path("/synthetic/codex"), Path("/synthetic/host"))
    config = NewawzConfig(
        "synthetic-provider", "synthetic-model", "SYNTHETIC_KEY", SYNTHETIC_CONFIG
    )
    output = tmp_path / "service-recovery.ndjson"
    peer = Path(__file__).parent / "fixtures/codex_app_server.py"
    monkeypatch.setattr(
        CodexRuntime,
        "command",
        lambda _, *, workspace: [sys.executable, str(peer), "normal", str(output)],
    )
    monkeypatch.setattr("milo_home.codex_process.time.time", lambda: case[-1][0])
    launcher = WorkLauncher(runtime, config, installed)
    task, window = abandon(
        (service_case, runtime, journal, launcher, output, ["normal"]),
        monkeypatch,
        mode="missing" if repair else "receipt",
    )
    before = output.read_bytes()
    assert WorkReconciliation(runtime).settle(case[0][1], window["id"], repair_receipts=repair)[
        "closed"
    ]
    assert WorkProgress(runtime.database).results(case[0][1], task["id"])["results"]
    assert os.geteuid() == 0 and len(fresh_stop) == 1 and output.read_bytes() == before
    assert all(p.stat().st_uid == account.pw_uid for p in path.parent.iterdir())
    assert all(p.stat().st_uid == 0 for p in journal.path.parent.iterdir())
    assert not journal.outstanding()


def test_v16_upgrade_preserves_finished_and_unknown_history_and_original_outputs(
    launch_case, fake_units, fresh_stop, monkeypatch
):
    case, runtime, _, launcher, _, _ = launch_case
    _, finished = reserve(case)
    launcher.run(case[0][1], finished["id"], credential="synthetic-secret")
    _, unknown = abandon(launch_case, monkeypatch)
    database = case[0][0]
    with database.connect(write=True) as connection:
        remove_reconciliation_schema(connection)
        connection.execute("PRAGMA user_version=16")
    before = snapshot(database)
    database.initialize()
    after = snapshot(database)
    assert all(after[name] == rows for name, rows in before.items())
    assert after["work_reconciliations"] == []
    assert WorkReconciliation(runtime).settle(case[0][1], unknown["id"])["closed"]


@pytest.mark.parametrize("change", ["deadline", "rewind"])
def test_settlement_uses_current_time_after_private_commit(
    launch_case, fake_units, fresh_stop, monkeypatch, change
):
    case, runtime, journal, _, _, _ = launch_case
    _, window = abandon(launch_case, monkeypatch)
    original = journal.reconcile_stream

    def reconcile(*args):
        original(*args)
        case[-1][0] = window["deadline_at"] if change == "deadline" else case[-1][0] - 1

    monkeypatch.setattr(journal, "reconcile_stream", reconcile)
    report = WorkReconciliation(runtime).settle(case[0][1], window["id"])
    if change == "deadline":
        assert report["closed"] and report["outcome"] == "timed_out"
    else:
        assert not report["closed"] and report["blocked_by"] == ["clock_rewind"]
    with case[0][0].connect() as connection:
        audits = connection.execute("SELECT checked_at FROM work_reconciliations").fetchall()
        assert [r[0] for r in audits] == ([case[-1][0]] if change == "deadline" else [])

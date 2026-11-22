import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_process import states
from test_work_runtime import SYNTHETIC_CONFIG
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case

from milo_home.codex_config import CodexRuntime, NewawzConfig
from milo_home.codex_rpc import DeliveryUnknown, ProtocolError
from milo_home.database_identity import ServiceDatabase
from milo_home.errors import ApiError
from milo_home.execution_budget import ExecutionBudget, ExecutionPolicy
from milo_home.execution_lock import execution_lock
from milo_home.work_delivery import WorkDelivery
from milo_home.work_process import WorkLauncher
from milo_home.work_prompt import PREAMBLE

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Host execution requires root")


def inputs(case):
    with case[0][0].connect() as connection:
        return [
            dict(row) for row in connection.execute("SELECT * FROM work_inputs ORDER BY sequence")
        ]


def starts(output):
    return [
        json.loads(line)["params"]
        for line in output.read_text().splitlines()
        if json.loads(line)["method"] == "turn/start"
    ]


def test_saved_inputs_run_serially_on_one_main_and_close_the_whole_family(launch_case, fake_units):
    case, _, journal, launcher, output, _ = launch_case
    task, window = reserve(case)
    work = case[0]
    for text in ["合成补充一", "合成补充二"]:
        work[5].append(work[1], task["id"], text, str(uuid4()))
    result = launcher.run(work[1], window["id"], credential="synthetic-secret")
    assert (result.outcome, result.turns) == ("completed", 3)
    assert states(case) == ("closed", "completed", 1, 3)
    assert [row["delivery"] for row in inputs(case)] == ["acknowledged"] * 3
    requests = starts(output)
    assert len(requests) == 3
    assert {row["threadId"] for row in requests} == {"synthetic-main"}
    snapshots = [json.loads(row["input"][0]["text"].removeprefix(PREAMBLE)) for row in requests]
    assert [data["input"]["sequence"] for data in snapshots] == [1, 2, 3]
    assert snapshots[1]["input"]["body"] == "合成补充一"
    assert len(fake_units[1]) == 1 and fake_units[1][0].process.poll() is not None
    assert not journal.outstanding() and not list(journal.path.parent.glob("*.env"))
    with pytest.raises(ApiError):
        launcher.run(work[1], window["id"], credential="synthetic-secret")
    assert len(fake_units[1]) == 1


@pytest.mark.parametrize(
    ("scenario", "outcome"),
    [
        ("failed-terminal", "failed"),
        ("error-then-failed", "failed"),
        ("interrupted-terminal", "cancelled"),
    ],
)
def test_non_success_terminal_ends_the_task_and_cancels_unsent_inputs(
    launch_case, fake_units, scenario, outcome
):
    case, _, journal, launcher, output, selected = launch_case
    task, window = reserve(case)
    work = case[0]
    work[5].append(work[1], task["id"], "不能偷偷再开一轮", str(uuid4()))
    selected[0] = scenario
    result = launcher.run(work[1], window["id"], credential="synthetic-secret")
    assert (result.outcome, result.turns) == (outcome, 1)
    assert states(case) == ("closed", outcome, 1, 1)
    assert [row["delivery"] for row in inputs(case)] == ["acknowledged", "cancelled"]
    assert len(starts(output)) == 1
    assert not journal.outstanding() and not list(journal.path.parent.glob("*.env"))


@pytest.mark.parametrize("scenario", ["eof", "error-no-terminal"])
def test_uncertain_turn_stops_family_but_preserves_unknown_and_cannot_resume(
    launch_case, fake_units, scenario
):
    case, _, journal, launcher, output, selected = launch_case
    task, window = reserve(case)
    selected[0] = scenario
    with pytest.raises(DeliveryUnknown if scenario == "eof" else ProtocolError):
        launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    assert states(case) == ("unknown", "needs_reconciliation", 1, 1)
    assert inputs(case)[0]["delivery"] == ("unknown" if scenario == "eof" else "acknowledged")
    assert fake_units[1][0].process.poll() is not None
    assert len(starts(output)) == 1
    assert bool(journal.outstanding()) == (scenario == "eof")
    assert not list(journal.path.parent.glob("*.env"))
    with pytest.raises(ApiError):
        launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    assert len(fake_units[1]) == 1


def test_stop_after_ack_does_not_misreport_a_completed_task(launch_case, fake_units, monkeypatch):
    case, _, journal, launcher, _, _ = launch_case
    task, window = reserve(case)
    original = WorkDelivery._acknowledge

    def stop(delivery):
        original(delivery)
        case[0][5].stop(case[0][1], task["id"], str(uuid4()))

    monkeypatch.setattr(WorkDelivery, "_acknowledge", stop)
    with pytest.raises(ApiError):
        launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    assert states(case)[:2] == ("unknown", "needs_reconciliation")
    assert inputs(case)[0]["delivery"] == "acknowledged"
    assert fake_units[1][0].process.poll() is not None
    assert not list(journal.path.parent.glob("*.env"))


def test_append_winning_before_first_claim_is_included(launch_case, fake_units, monkeypatch):
    case, _, _, launcher, output, _ = launch_case
    task, window = reserve(case)
    original = WorkDelivery._prepare
    appended = []

    def append(delivery, input_id):
        original(delivery, input_id)
        if not appended:
            extra, _ = case[0][5].append(case[0][1], task["id"], "合成启动竞争补充", str(uuid4()))
            appended.append(extra)

    monkeypatch.setattr(WorkDelivery, "_prepare", append)
    result = launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    assert (result.outcome, result.turns) == ("completed", 2)
    assert len(starts(output)) == 2
    assert [row["delivery"] for row in inputs(case)] == ["acknowledged"] * 2


def test_exhausted_turn_budget_does_not_send_the_next_saved_input(launch_case, fake_units):
    case, _, journal, launcher, output, _ = launch_case
    task, window = reserve(case)
    case[0][5].append(case[0][1], task["id"], "下一轮超额", str(uuid4()))
    ExecutionBudget(case[0][0], lambda: case[-1][0]).configure(
        ExecutionPolicy(daily_launches=1, daily_seconds=None)
    )
    with pytest.raises(ApiError, match="daily_budget_exhausted"):
        launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    assert len(starts(output)) == 1
    assert states(case)[:2] == ("unknown", "needs_reconciliation")
    assert [row["delivery"] for row in inputs(case)] == ["acknowledged", "queued"]
    assert not list(journal.path.parent.glob("*.env"))


def test_stop_changes_during_cleanup_win_over_turn_success(launch_case, fake_units, monkeypatch):
    case, _, _, launcher, _, _ = launch_case
    task, window = reserve(case)
    original = fake_units[0].stop

    def stop(unit):
        confirmed = original(unit)
        case[0][5].stop(case[0][1], task["id"], str(uuid4()))
        return confirmed

    monkeypatch.setattr(fake_units[0], "stop", stop)
    result = launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    assert (result.outcome, result.turns) == ("cancelled", 1)
    assert states(case)[:2] == ("closed", "cancelled")


def test_real_database_account_cannot_expose_private_runtime_during_delivery(
    runtime_case, owned_database, fake_units, monkeypatch, tmp_path
):
    case, runtime, journal = runtime_case
    task, window = reserve(case)
    path, account = owned_database
    copied = path.parent / "seeded.sqlite3"
    case[0][0].backup(copied)
    os.chown(copied, account.pw_uid, account.pw_gid)
    os.replace(copied, path)
    runtime.database = ServiceDatabase(path, account.pw_name)
    with execution_lock(runtime.database) as lease:
        runtime.bind(case[0][1], case[0][-1]["id"], lease=lease)
    installed = CodexRuntime(Path("/synthetic/codex"), Path("/synthetic/host"))
    config = NewawzConfig(
        "synthetic-provider", "synthetic-model", "SYNTHETIC_KEY", SYNTHETIC_CONFIG
    )
    output = tmp_path / "service-protocol.ndjson"
    peer = Path(__file__).parent / "fixtures/codex_app_server.py"
    monkeypatch.setattr(
        CodexRuntime,
        "command",
        lambda _, *, workspace: [sys.executable, str(peer), "normal", str(output)],
    )
    monkeypatch.setattr("milo_home.codex_process.time.time", lambda: case[-1][0])
    launcher = WorkLauncher(runtime, config, installed)
    result = launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    assert result.outcome == "completed" and os.geteuid() == 0
    assert all(p.stat().st_uid == account.pw_uid for p in path.parent.iterdir())
    assert all(p.stat().st_uid == 0 for p in journal.path.parent.iterdir())
    assert not journal.outstanding()


@pytest.mark.skipif(not Path("/run/systemd/system").exists(), reason="Requires systemd")
def test_real_systemd_runs_a_synthetic_task_and_confirms_family_exit(launch_case):
    case, _, journal, launcher, output, _ = launch_case
    _, window = reserve(case, seconds=20)
    result = launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    assert (result.outcome, result.turns) == ("completed", 1)
    assert states(case) == ("closed", "completed", 1, 1)
    assert len(starts(output)) == 1
    assert not journal.outstanding() and not list(journal.path.parent.glob("*.env"))

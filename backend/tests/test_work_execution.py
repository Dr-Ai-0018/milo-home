import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from test_cli_entrypoints import command
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_work_cli import runtime_case as runtime_case
from test_work_coordinator import starts
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.cli import main
from milo_home.codex_config import CodexRuntime, NewawzConfig
from milo_home.codex_rpc import ProtocolError
from milo_home.database_identity import ServiceDatabase
from milo_home.errors import ApiError
from milo_home.execution_lock import execution_lock
from milo_home.work_process import WorkLauncher
from milo_home.work_progress import WorkProgress
from milo_home.work_runtime import WorkRuntime

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root execution entry")


def invoke(monkeypatch, launch_case, task_id, *, timeout=60, owner=None, account=None):
    case, runtime, journal, launcher, _, _ = launch_case
    monkeypatch.setenv("MILO_DATABASE", str(runtime.database.path))
    monkeypatch.setenv("SYNTHETIC_KEY", "synthetic-secret")
    monkeypatch.setattr("milo_home.work_execution.NewawzConfig.load", lambda: launcher.config)
    monkeypatch.setattr(
        "milo_home.work_execution.WorkRuntime",
        lambda database, journal: WorkRuntime(database, journal, runtime.clock),
    )
    account_options = ["--database-user", account] if account else []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "milo-home",
            *account_options,
            "--run-directory",
            str(journal.path.parent),
            "work",
            "run",
            "--owner",
            owner or case[0][1],
            "--task",
            task_id,
            "--timeout",
            str(timeout),
            "--execute",
        ],
    )
    main()


def counts(database):
    with database.connect() as connection:
        return [
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("execution_windows", "execution_start_intents", "execution_permits")
        ]


def test_cli_executes_saved_inputs_on_registered_main_and_cannot_repeat(
    launch_case, fake_units, monkeypatch, capsys
):
    case, runtime, journal, _, output, _ = launch_case
    task = create(case[0])
    case[0][5].append(case[0][1], task["id"], "合成补充", str(uuid4()))
    invoke(monkeypatch, launch_case, task["id"])
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "completed" and report["turns"] == 2
    assert counts(runtime.database) == [1, 1, 2]
    assert {request["threadId"] for request in starts(output)} == {"synthetic-main"}
    results = WorkProgress(runtime.database).results(case[0][1], task["id"])["results"]
    assert len(results) == 2 and results[0]["output"]["answer"]["body"] == "合成结果"
    assert not journal.outstanding() and not list(journal.path.parent.glob("*.env"))
    assert fake_units[1][0].process.poll() is not None
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, launch_case, task["id"])
    assert exit_.value.code == 1 and len(fake_units[1]) == 1


@pytest.mark.parametrize(
    "failure", ["binding", "config", "version", "private", "owner", "policy", "schedule"]
)
def test_preflight_failures_never_reserve_or_spawn(
    launch_case, fake_units, monkeypatch, capsys, failure
):
    case, runtime, journal, _, output, _ = launch_case
    task = create(case[0])
    if failure == "binding":
        with journal._connect() as connection:
            connection.execute("DELETE FROM business_binding")
    elif failure == "config":
        (journal.path.parent / "config.toml").write_bytes(b"synthetic changed config")
    elif failure == "version":

        def fail(_, *, workspace):
            raise ProtocolError("synthetic-private-installation-error")

        monkeypatch.setattr(CodexRuntime, "command", fail)
    elif failure == "private":
        journal.open_stream("milo-work-" + "d" * 32 + ".service")
    elif failure == "policy":
        with runtime.database.connect(write=True) as connection:
            connection.execute("UPDATE execution_policy SET policy=NULL")
    elif failure == "schedule":
        case[2].pause(True)
    with pytest.raises(SystemExit) as exit_:
        invoke(
            monkeypatch, launch_case, task["id"], owner=case[0][2] if failure == "owner" else None
        )
    assert exit_.value.code == 1
    captured = capsys.readouterr()
    assert "synthetic-private" not in captured.out + captured.err and not captured.out
    assert counts(runtime.database) == [0, 0, 0] and not fake_units[1] and not output.exists()


def test_missing_credential_refuses_before_reservation(launch_case, fake_units, monkeypatch):
    case, runtime, _, launcher, output, _ = launch_case
    task = create(case[0])
    with pytest.raises(ValueError, match="credential"):
        launcher.run_task(case[0][1], task["id"], 60, credential="")
    assert counts(runtime.database) == [0, 0, 0] and not output.exists() and not fake_units[1]


def test_lock_conflict_never_reserves_a_window(launch_case, fake_units):
    case, runtime, _, launcher, _, _ = launch_case
    task = create(case[0])
    with execution_lock(runtime.database), pytest.raises(ApiError, match="worker_busy"):
        launcher.run_task(case[0][1], task["id"], 60, credential="synthetic-secret")
    assert counts(runtime.database) == [0, 0, 0] and not fake_units[1]


@pytest.mark.parametrize("claimed", [False, True])
def test_failed_claim_only_releases_window_when_no_durable_intention(
    launch_case, fake_units, monkeypatch, claimed
):
    case, runtime, journal, launcher, _, _ = launch_case
    task = create(case[0])

    def fail(*args, **kwargs):
        raise OSError("synthetic commit boundary")

    if claimed:
        monkeypatch.setattr(journal, "open_work_stream", fail)
    else:
        monkeypatch.setattr(runtime, "claim_start", fail)
    with pytest.raises(OSError):
        launcher.run_task(case[0][1], task["id"], 60, credential="synthetic-secret")
    assert counts(runtime.database) == [1, int(claimed), 0] and not fake_units[1]
    with runtime.database.connect() as connection:
        state = connection.execute("SELECT state FROM execution_windows").fetchone()[0]
    assert state == ("unknown" if claimed else "closed")
    assert case[0][5].get(case[0][1], task["id"])["status"] == (
        "needs_reconciliation" if claimed else "failed"
    )


def test_signal_before_process_launch_records_cancel_and_cleans(
    launch_case, fake_units, monkeypatch, capsys
):
    case, runtime, journal, launcher, _, _ = launch_case
    task = create(case[0])
    original = WorkRuntime.claim_start

    def signal_after_claim(self, *args, **kwargs):
        intent = original(self, *args, **kwargs)
        os.kill(os.getpid(), signal.SIGTERM)
        return intent

    monkeypatch.setattr(WorkRuntime, "claim_start", signal_after_claim)
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, launch_case, task["id"])
    assert exit_.value.code == 1 and signal.getsignal(signal.SIGTERM) == previous
    assert not fake_units[1] and not journal.outstanding()
    assert case[0][5].get(case[0][1], task["id"])["status"] == "cancelled"
    assert not list(journal.path.parent.glob("*.env"))


def test_signal_during_running_turn_stops_process_and_keeps_unknown(
    launch_case, fake_units, monkeypatch, capsys
):
    from milo_home.work_delivery import WorkDelivery

    case, runtime, journal, _, _, selected = launch_case
    task = create(case[0])
    selected[0] = "interrupt"
    original = WorkDelivery.wait

    def signal_before_wait(self):
        os.kill(os.getpid(), signal.SIGINT)
        return original(self)

    monkeypatch.setattr(WorkDelivery, "wait", signal_before_wait)
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, launch_case, task["id"])
    assert exit_.value.code == 1
    assert fake_units[1][0].process.poll() is not None
    assert case[0][5].get(case[0][1], task["id"])["status"] == "needs_reconciliation"
    with runtime.database.connect() as connection:
        assert connection.execute("SELECT state FROM execution_permits").fetchone()[0] == "unknown"
    assert not list(journal.path.parent.glob("*.env"))


@pytest.mark.parametrize("timeout", [0, 3601])
def test_cli_invalid_deadline_has_no_side_effect(launch_case, fake_units, monkeypatch, timeout):
    case, runtime, _, _, _, _ = launch_case
    task = create(case[0])
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, launch_case, task["id"], timeout=timeout)
    assert exit_.value.code == 1 and counts(runtime.database) == [0, 0, 0]


def test_signal_after_last_terminal_preserves_result_and_cancels_task(
    launch_case, fake_units, monkeypatch, capsys
):
    import milo_home.work_process as module

    case, runtime, _, _, _, _ = launch_case
    task = create(case[0])
    original = module.run_inputs

    def finish_then_signal(*args, **kwargs):
        result = original(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGTERM)
        return result

    monkeypatch.setattr(module, "run_inputs", finish_then_signal)
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, launch_case, task["id"])
    assert exit_.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "cancelled" and report["turns"] == 1
    result = WorkProgress(runtime.database).results(case[0][1], task["id"])["results"][0]
    assert result["outcome"] == "completed"
    assert fake_units[1][0].process.poll() is not None


def test_lost_response_is_sanitized_and_cannot_reexecute(
    launch_case, fake_units, monkeypatch, capsys
):
    case, runtime, _, _, _, selected = launch_case
    task = create(case[0])
    selected[0] = "eof"
    for _ in range(2):
        with pytest.raises(SystemExit) as exit_:
            invoke(monkeypatch, launch_case, task["id"])
        assert exit_.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err and not captured.out
    assert len(fake_units[1]) == 1 and counts(runtime.database) == [1, 1, 1]
    assert case[0][5].get(case[0][1], task["id"])["status"] == "needs_reconciliation"


def test_publication_failure_preserves_completed_task_without_reexecution(
    launch_case, fake_units, monkeypatch, capsys
):
    from milo_home.work_recovery import WorkRecovery

    case, runtime, _, _, output, _ = launch_case
    task = create(case[0])
    original = WorkRecovery.replay_results

    def fail(*args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(WorkRecovery, "replay_results", fail)
    with pytest.raises(SystemExit):
        invoke(monkeypatch, launch_case, task["id"])
    assert case[0][5].get(case[0][1], task["id"])["status"] == "completed"
    before = output.read_bytes()
    assert original(WorkRecovery(runtime), case[0][1], next_window(runtime.database)) == 1
    assert output.read_bytes() == before and len(fake_units[1]) == 1


def next_window(database):
    with database.connect() as connection:
        return connection.execute("SELECT id FROM execution_windows").fetchone()[0]


def test_service_account_run_keeps_private_files_root_owned(
    runtime_case, owned_database, fake_units, monkeypatch, capsys, tmp_path
):
    from test_work_runtime import SYNTHETIC_CONFIG

    case, original_runtime, journal = runtime_case
    task = create(case[0])
    path, account = owned_database
    copied = path.parent / "seeded.sqlite3"
    original_runtime.database.backup(copied)
    os.chown(copied, account.pw_uid, account.pw_gid)
    os.replace(copied, path)
    runtime = WorkRuntime(ServiceDatabase(path, account.pw_name), journal, original_runtime.clock)
    with execution_lock(runtime.database) as lease:
        runtime.bind(case[0][1], case[0][-1]["id"], lease=lease)
    config = NewawzConfig(
        "synthetic-provider", "synthetic-model", "SYNTHETIC_KEY", SYNTHETIC_CONFIG
    )
    output = tmp_path / "requests.ndjson"
    peer = Path(__file__).parent / "fixtures/codex_app_server.py"
    monkeypatch.setattr(
        CodexRuntime,
        "command",
        lambda _, *, workspace: [sys.executable, str(peer), "normal", str(output)],
    )
    monkeypatch.setattr("milo_home.codex_process.time.time", lambda: case[-1][0])
    launcher = WorkLauncher(runtime, config, journal.installation())
    assembled = (case, runtime, journal, launcher, output, ["normal"])
    invoke(monkeypatch, assembled, task["id"], account=account.pw_name)
    assert json.loads(capsys.readouterr().out)["outcome"] == "completed"
    assert all(p.stat().st_uid == account.pw_uid for p in path.parent.iterdir())
    assert all(p.stat().st_uid == 0 for p in journal.path.parent.iterdir())
    assert os.geteuid() == 0


def test_actual_death_after_reservation_keeps_occupancy_without_reclaim(launch_case, fake_units):
    case, runtime, journal, launcher, _, _ = launch_case
    task = create(case[0])
    program = """
import os, sys
from pathlib import Path
from milo_home.database import Database
from milo_home.codex_journal import PrivateJournal
from milo_home.work_runtime import WorkRuntime
from milo_home.work_process import WorkLauncher
runtime = WorkRuntime(Database(Path(sys.argv[1])),
                      PrivateJournal(Path(sys.argv[2]), initialize=False),
                      lambda: int(sys.argv[5]))
launcher = WorkLauncher(runtime, None, None)
launcher._prepare = lambda credential, *, execute: ([], b'', None, False)
launcher._open_prepared = lambda *args, **kwargs: os._exit(96)
launcher.run_task(sys.argv[3], sys.argv[4], 60, credential='synthetic')
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(runtime.database.path),
            str(journal.path.parent),
            case[0][1],
            task["id"],
            str(case[-1][0]),
        ],
        capture_output=True,
        timeout=6,
    )
    assert result.returncode == 96 and counts(runtime.database) == [1, 0, 0]
    with pytest.raises(ApiError, match="run_busy"):
        launcher.run_task(case[0][1], task["id"], 60, credential="synthetic-secret")
    assert not fake_units[1]


def test_signal_and_revocation_still_settle_unknown_occupancy(launch_case, fake_units, monkeypatch):
    from milo_home.work_delivery import WorkDelivery

    case, runtime, journal, _, _, selected = launch_case
    task = create(case[0])
    selected[0] = "interrupt"
    original = WorkDelivery.wait

    def revoke_and_signal(self):
        case[0][4].revoke(case[0][1])
        os.kill(os.getpid(), signal.SIGTERM)
        return original(self)

    monkeypatch.setattr(WorkDelivery, "wait", revoke_and_signal)
    with pytest.raises(SystemExit):
        invoke(monkeypatch, launch_case, task["id"])
    with runtime.database.connect() as connection:
        assert (
            connection.execute("SELECT status FROM work_tasks").fetchone()[0]
            == "needs_reconciliation"
        )
        assert connection.execute("SELECT state FROM execution_windows").fetchone()[0] == "unknown"
    assert fake_units[1][0].process.poll() is not None
    assert not list(journal.path.parent.glob("*.env"))


def test_unclaimed_signal_and_revocation_close_without_launch(launch_case, fake_units, monkeypatch):
    case, runtime, _, launcher, _, _ = launch_case
    task = create(case[0])
    requested = [False]
    launcher.stop_requested = lambda: requested[0]

    def revoke_before_claim(*args, **kwargs):
        requested[0] = True
        case[0][4].revoke(case[0][1])
        raise ApiError(403, "synthetic_revocation", "合成撤权")

    monkeypatch.setattr(runtime, "claim_start", revoke_before_claim)
    with pytest.raises(ApiError):
        launcher.run_task(case[0][1], task["id"], 60, credential="synthetic-secret")
    with runtime.database.connect() as connection:
        assert connection.execute("SELECT status FROM work_tasks").fetchone()[0] == "cancelled"
        assert connection.execute("SELECT state FROM execution_windows").fetchone()[0] == "closed"
    assert counts(runtime.database) == [1, 0, 0] and not fake_units[1]


@pytest.mark.parametrize(
    "field,value",
    [
        ("codex_home", "/synthetic/elsewhere"),
        ("version", "other-version"),
        ("cli", 42),
        ("tool_host", None),
        (None, []),
    ],
)
def test_installation_cannot_be_selected_from_another_runtime(
    launch_case, fake_units, monkeypatch, capsys, field, value
):
    case, runtime, journal, _, _, _ = launch_case
    task = create(case[0])
    with journal._connect() as connection:
        row = connection.execute("SELECT value FROM binding").fetchone()[0]
        data = json.loads(row)
        if field is None:
            data = value
        else:
            data[field] = value
        connection.execute("UPDATE binding SET value=?", (json.dumps(data),))
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, launch_case, task["id"])
    assert exit_.value.code == 1
    assert capsys.readouterr().err == "Configuration or file check failed.\n"
    assert counts(runtime.database) == [0, 0, 0] and not fake_units[1]


@pytest.mark.parametrize(
    "scenario,outcome",
    [
        ("failed-terminal", "failed"),
        ("interrupted-terminal", "cancelled"),
    ],
)
def test_cli_reports_unsuccessful_terminal_without_sending_supplements(
    launch_case, fake_units, monkeypatch, capsys, scenario, outcome
):
    case, runtime, _, _, output, selected = launch_case
    task = create(case[0])
    case[0][5].append(case[0][1], task["id"], "合成未投递补充", str(uuid4()))
    selected[0] = scenario
    with pytest.raises(SystemExit) as exit_:
        invoke(monkeypatch, launch_case, task["id"])
    assert exit_.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == outcome and report["turns"] == 1
    assert len(starts(output)) == 1
    inputs = case[0][5].inputs(case[0][1], task["id"], 0, 50)["inputs"]
    assert [row["delivery"] for row in inputs] == ["acknowledged", "cancelled"]


def test_execute_flag_is_required_before_opening_data(tmp_path):
    path = tmp_path / "must-not-create.sqlite3"
    result = command(
        path, "work", "run", "--owner", str(uuid4()), "--task", str(uuid4()), "--timeout", "60"
    )
    assert result.returncode == 2 and "--execute" in result.stderr
    assert not path.exists()


@pytest.mark.skipif(not os.path.exists("/run/systemd/system"), reason="Requires systemd")
def test_actual_systemd_synthetic_cli_run_stops_and_publishes(launch_case, monkeypatch, capsys):
    case, runtime, journal, _, output, _ = launch_case
    task = create(case[0])
    invoke(monkeypatch, launch_case, task["id"])
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "completed" and report["turns"] == 1
    assert len(starts(output)) == 1
    with runtime.database.connect() as connection:
        stream = connection.execute("SELECT stream_id FROM execution_start_intents").fetchone()[0]
    private = journal.work_snapshot(stream)
    ready = [e for _, e in private["entries"] if e.get("phase") == "ready"]
    assert len(ready) == 1 and ready[0]["main_pid"] > 0 and ready[0]["boot_id"]
    assert private["stream"]["state"] == "closed"
    assert not list(journal.path.parent.glob("*.env"))

"""Use the public command entry to observe actual bound synthetic materials."""

import json
import sys
from types import SimpleNamespace

import pytest
from test_execution_control import execution_case as execution_case
from test_journal_retention_integration import managed_work as managed_work
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case

from milo_home import maintenance_commands
from milo_home.cli import main
from milo_home.cli_arguments import build_parser


@pytest.mark.parametrize("source", ["environment", "toml"])
def test_public_collect_and_show_do_not_start_work(
    managed_work, monkeypatch, capsys, private_runtime_directory, source
):
    _, runtime, journal, _, requests, _ = managed_work
    root = private_runtime_directory("milo-maintenance-cli-")
    public = root / "published"
    public.mkdir(mode=0o750)
    monkeypatch.setenv("MILO_DATABASE", str(runtime.database.path))
    monkeypatch.setenv("MILO_MAINTENANCE_FILE", str(public / "summary.json"))
    monkeypatch.setenv("MILO_MAINTENANCE_OWNER", journal.business_binding().owner_id)
    prefix = []
    arguments = [
        "--run-directory",
        str(journal.path.parent),
        "maintenance",
        "collect",
        "--workspace",
        str(journal.binding().cwd),
        "--success-file",
        str(root / "missing"),
    ]
    if source == "toml":
        configuration = root / "site.toml"
        configuration.write_text(
            f'[app]\ndatabase="{runtime.database.path}"\n'
            f'[maintenance]\nfile="{public}/summary.json"\n'
            f'owner="{journal.business_binding().owner_id}"\n'
            f'success_file="{root}/missing"\n'
            f'[execution]\nrun_directory="{journal.path.parent}"\n'
            f'workspace="{journal.binding().cwd}"\n'
        )
        configuration.chmod(0o640)
        prefix = ["--config", str(configuration)]
        arguments = ["maintenance", "collect"]
        for key in ("MILO_DATABASE", "MILO_MAINTENANCE_FILE", "MILO_MAINTENANCE_OWNER"):
            monkeypatch.delenv(key)
    monkeypatch.setattr("milo_home.maintenance_commands.systemd_unit_runner", lambda *_: {})
    monkeypatch.setattr(
        sys,
        "argv",
        ["milo-home", *prefix, *arguments],
    )
    existed = requests.exists()
    traffic = requests.read_bytes() if existed else None
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["maintenance"] is None
    assert len(report["summary"]["capacity"]) == 10
    assert report["summary"]["backups"][0]["state"] == "unknown"
    monkeypatch.setattr(sys, "argv", ["milo-home", *prefix, "maintenance", "show"])
    main()
    shown = json.loads(capsys.readouterr().out)
    assert shown["observed_at"] == report["summary"]["observed_at"]
    assert requests.exists() == existed
    assert not existed or requests.read_bytes() == traffic
    assert journal.list_streams() == []


def test_help_and_missing_mutation_confirmation_do_not_open_data(tmp_path, monkeypatch):
    database = tmp_path / "not-created.sqlite3"
    monkeypatch.setenv("MILO_DATABASE", str(database))
    monkeypatch.setattr(sys, "argv", ["milo-home", "maintenance", "--help"])
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 0 and not database.exists()
    with pytest.raises(SystemExit) as stopped:
        build_parser().parse_args(["maintenance", "run", "--workspace", str(tmp_path)])
    assert stopped.value.code == 2 and not database.exists()


def test_interrupted_capture_records_failure_and_restores_signal_handler(monkeypatch):
    recorded = []
    handlers = []
    previous = object()

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(maintenance_commands, "load_backup_policy", lambda _: object())
    monkeypatch.setattr(maintenance_commands, "require_selection", lambda *_: "selection")
    monkeypatch.setattr(maintenance_commands.signal, "getsignal", lambda _: previous)
    monkeypatch.setattr(
        maintenance_commands.signal, "signal", lambda _, value: handlers.append(value)
    )
    monkeypatch.setattr(maintenance_commands, "capture_for_maintenance", interrupted)
    monkeypatch.setattr(maintenance_commands, "record_fact", lambda *args: recorded.append(args))
    case = SimpleNamespace(attachments=None, public_assets=None, database=object(), home=object())
    args = SimpleNamespace(
        policy=None,
        reply_directory=None,
        maintenance_action="capture",
        workspace=None,
        out=None,
        writers_stopped=True,
    )
    report = maintenance_commands.backup_action(case, args)
    assert report["status"] == "failed" and report["reason"] == "interrupted"
    assert recorded[0][2:] == ("paired", report)
    assert handlers[-1] is previous

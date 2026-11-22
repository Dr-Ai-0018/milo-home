"""A reception choice during preparation stays a normal intake outcome, never a service fault."""

import json

from test_execution_control import execution_case as execution_case
from test_reception import request
from test_work_cli import runtime_case as runtime_case
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_receiver import invoke
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.automatic_services import AutomaticServices
from milo_home.reception import save


def test_recurring_work_rechecks_after_preparation_without_latching_fault(
    launch_case, fake_units, monkeypatch, capsys
):
    case, runtime, journal, launcher, output, _ = launch_case
    database, owner, _, _, _, tasks, session = case[0]
    task = create(case[0])
    AutomaticServices(database, runtime.clock).configure(
        "work", owner, "milo-home-work.timer", session=session["id"], enabled=True
    )
    monkeypatch.setattr("milo_home.work_service_control.timer_active", lambda _: True)
    original = launcher._prepare

    def prepare(self, credential, *, execute):
        result = original(credential, execute=execute)
        with database.connect(write=True) as connection:
            save(connection, owner, request("work"), int(runtime.clock()))
        return result

    monkeypatch.setattr(type(launcher), "_prepare", prepare)
    credential = journal.path.parent / "synthetic.key"
    credential.write_text("synthetic-secret", encoding="utf-8")
    credential.chmod(0o600)
    invoke(monkeypatch, launch_case, once=True, credential=credential)
    assert json.loads(capsys.readouterr().out) == {"outcome": "reception_closed", "turns": 0}
    assert not fake_units[1] and not output.exists()
    assert tasks.get(owner, task["id"])["status"] == "queued"
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM execution_windows").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT fault_code FROM automatic_services WHERE kind='work'"
            ).fetchone()[0]
            is None
        )

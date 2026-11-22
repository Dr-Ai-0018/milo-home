import json

import pytest
from test_execution_control import execution_case as execution_case
from test_work_cli import runtime_case as runtime_case
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_receiver import invoke
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.automatic_queue import automatic_choice
from milo_home.automatic_services import AutomaticServices
from milo_home.management_lease import current_boot
from milo_home.work_service_control import observe_work_entry


@pytest.mark.parametrize("failure", ["credential", "binding", "config"])
def test_bad_registered_work_latches_and_stops_blocking_reply(
    launch_case, fake_units, monkeypatch, capsys, failure
):
    case, runtime, journal, _, _, _ = launch_case
    database, owner = runtime.database, case[0][1]
    session = case[0][-1]["id"]
    services = AutomaticServices(database, runtime.clock)
    services.configure("work", owner, "milo-home-work.timer", session=session, enabled=True)
    services.configure("reply", owner, "milo-home-reply.timer", enabled=True)
    services.observe("reply", owner, timer_active=True)
    monkeypatch.setattr("milo_home.work_service_control.timer_active", lambda unit: True)
    monkeypatch.setattr(
        "milo_home.work_execution.observe_work_entry",
        lambda db, user, selected: observe_work_entry(db, user, selected, runtime.clock),
    )
    task = create(case[0])
    key = journal.path.parent / "service.key"
    key.write_text("synthetic-secret")
    key.chmod(0o600)
    if failure == "binding":
        with journal._connect() as connection:
            connection.execute("DELETE FROM business_binding")
    elif failure == "config":

        def broken_config():
            raise ValueError("synthetic bad config")

        # invoke installs its own config loader, so fail private installation instead.
        monkeypatch.setattr(
            "milo_home.work_execution.PrivateJournal.installation", lambda _: broken_config()
        )
    with pytest.raises(SystemExit):
        invoke(
            monkeypatch,
            launch_case,
            once=True,
            credential=None if failure == "credential" else key,
        )
    capsys.readouterr()
    with database.connect(write=True) as connection:
        fault = connection.execute(
            "SELECT fault_code FROM automatic_services WHERE kind='work'"
        ).fetchone()[0]
        assert fault == ("needs_reconciliation" if failure == "binding" else "configuration_error")
        choice = automatic_choice(
            connection,
            "reply",
            owner,
            int(runtime.clock()),
            current_boot(),
            reply_candidates=[{"ready_at": task["created_at"] + 1, "message_ids": ["later"]}],
        )
        assert choice["kind"] == "reply"
    invoke(monkeypatch, launch_case, once=True, credential=key)
    assert json.loads(capsys.readouterr().out)["outcome"] == "service_disabled_or_faulted"
    assert not fake_units[1]

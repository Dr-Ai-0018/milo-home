import pytest
from test_execution_control import execution_case as execution_case
from test_work_delivery import delivery_case as delivery_case
from test_work_delivery import rows
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.codex_rpc import ProtocolError
from milo_home.journal_capacity import ENTRY_LIMIT, ORDINARY_RESERVE, STREAM_LIMIT


def test_capacity_failure_does_not_reserve_or_consume_turn_permit(delivery_case):
    case, _, journal, _, task, _, open_ = delivery_case
    delivery, _, output = open_()
    with journal._connect() as connection:
        connection.execute(
            "UPDATE streams SET bytes=? WHERE id=?",
            (STREAM_LIMIT - ORDINARY_RESERVE - ENTRY_LIMIT + 1, delivery.intent.stream_id),
        )
    with case[0][0].connect() as connection:
        input_id = connection.execute(
            "SELECT id FROM work_inputs WHERE task_id=?", (task["id"],)
        ).fetchone()[0]
    before = rows(case)
    sent = output.read_bytes()
    with pytest.raises(ProtocolError, match="cleanup space reserved"):
        delivery.start(input_id)
    assert rows(case) == before
    assert output.read_bytes() == sent


def test_new_work_capacity_refuses_before_window_or_permit(launch_case, fake_units):
    case, runtime, journal, launcher, output, _ = launch_case
    task = create(case[0])
    with journal._connect() as connection:
        for number in range(1, 5):
            connection.execute(
                "INSERT INTO streams VALUES (?,?,'closed',?)",
                (f"{number:032x}", f"milo-work-{number:032x}.service", 7 * 1024 * 1024),
            )
    with pytest.raises(ProtocolError, match="archival"):
        launcher.run_task(case[0][1], task["id"], 60, credential="synthetic-secret")
    with runtime.database.snapshot() as connection:
        for table in ("execution_windows", "execution_start_intents", "execution_permits"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert not output.exists() and not fake_units[1]

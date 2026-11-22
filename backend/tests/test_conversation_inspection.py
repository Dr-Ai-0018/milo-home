import pytest
from test_dispatch import make_case

from milo_home.backup_rehearsal import rehearse_backup
from milo_home.database import Database
from milo_home.database_inspection import inspect_database


@pytest.mark.parametrize(
    "trigger", ["threads_default_conversation", "messages_conversation_update"]
)
def test_missing_constraint_triggers_fail_inspection_without_repair(tmp_path, trigger):
    database = Database(tmp_path / "missing-trigger.sqlite3")
    database.initialize()
    with database.connect(write=True) as connection:
        connection.execute(f"DROP TRIGGER {trigger}")
    before = database.path.read_bytes()
    report = inspect_database(database.path)
    assert not report["database_ok"]
    assert not report["conversation_constraints_ok"]
    assert report["attention"] == ["conversation_constraints_failed"]
    assert database.path.read_bytes() == before


@pytest.mark.parametrize("table", ["messages", "runs"])
def test_invalid_scope_is_detected_even_when_constraint_triggers_exist(client, tmp_path, table):
    _, runs, dispatch, _ = make_case(client)
    dispatch.reserve(60)
    database = runs.database
    trigger = f"{table}_conversation_update"
    with database.connect(write=True) as connection:
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (trigger,)
        ).fetchone()[0]
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(f"UPDATE {table} SET conversation_id = NULL")
        connection.execute(definition)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    report = inspect_database(database.path)
    assert report["foreign_keys_ok"]
    assert not report["database_ok"]
    assert not report["conversation_constraints_ok"]
    with pytest.raises(ValueError, match="failed inspection"):
        rehearse_backup(database.path, tmp_path / "rehearsal.sqlite3")

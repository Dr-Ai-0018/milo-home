import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from test_work_migration import snapshot, version_ten

from milo_home import database as database_module
from milo_home.execution_control import execution_busy

PREDECESSOR = "original-attempt"
SESSION = "original-session"
RETIREMENT = "INSERT INTO work_main_attempt_retirements VALUES (?,?)"
GRANT = "INSERT INTO work_main_attempt_grants VALUES (?,?,?)"
SUCCESSOR = (
    "INSERT INTO work_main_creations "
    "(id,session_id,choice_digest,owner_revision,workspace_revision,created_at,"
    "deadline_at,state,supersedes_id,authorization_id) VALUES (?,?,?,1,1,100,160,?,?,?)"
)


@pytest.fixture
def original_attempt(tmp_path, monkeypatch):
    database = version_ten(tmp_path)
    with monkeypatch.context() as context:
        context.setattr(
            database_module,
            "MIGRATIONS",
            tuple(m for m in database_module.MIGRATIONS if m[0] <= 18),
        )
        database.initialize()
    with database.connect(write=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18
        connection.execute("UPDATE runs SET status='failed',ended_at=3 WHERE id='active'")
        connection.execute("INSERT INTO work_access VALUES ('owner',1,1,1)")
        connection.execute(
            "INSERT INTO workspaces VALUES ('workspace',1,'synthetic',?,1,1,1)", (str(tmp_path),)
        )
        connection.execute(
            "INSERT INTO work_sessions VALUES (?,1,'owner','workspace',1)", (SESSION,)
        )
        connection.execute(
            "INSERT INTO work_main_creations VALUES (?,?,?,1,1,1,61,'unknown')",
            (PREDECESSOR, SESSION, "0" * 64),
        )
    return database


def test_v18_migration_preserves_every_original_value_and_does_not_infer_receipt(original_attempt):
    before = snapshot(original_attempt)
    original_attempt.initialize()
    after = snapshot(original_attempt)
    assert after["work_main_attempt_retirements"] == after["work_main_attempt_grants"] == []
    row = after["work_main_creations"][0]
    assert row.pop("supersedes_id") is row.pop("authorization_id") is None
    assert all(after[name] == rows for name, rows in before.items())
    with original_attempt.connect() as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == database_module.CURRENT_SCHEMA_VERSION
        )
        assert execution_busy(connection)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_late_v19_migration_failure_restores_original_table_and_guards(
    original_attempt, tmp_path, monkeypatch
):
    before = snapshot(original_attempt)
    resources = database_module.files("milo_home")
    original_sql = resources.joinpath("migrations", "019_main_attempts.sql").read_text()
    broken = tmp_path / "late-failure.sql"
    broken.write_text(original_sql + "\nINSERT INTO absent_table VALUES (1);\n")

    class Resources:
        def joinpath(self, *parts):
            assert parts[-1] == "019_main_attempts.sql"
            return broken

    with monkeypatch.context() as context:
        context.setattr(database_module, "files", lambda _: Resources())
        with pytest.raises(sqlite3.OperationalError, match="absent_table"):
            original_attempt.initialize()
    assert snapshot(original_attempt) == before
    with original_attempt.connect(write=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18
        with pytest.raises(sqlite3.IntegrityError, match="main creation already occupied"):
            connection.execute("UPDATE runs SET status='queued' WHERE id='active'")


def test_sql_successor_needs_closed_receipt_exact_grant_and_held_status(original_attempt):
    original_attempt.initialize()
    with original_attempt.connect(write=True) as connection:
        successor = ("successor", SESSION, "1" * 64, "held", PREDECESSOR, "grant")
        with pytest.raises(sqlite3.IntegrityError, match="requires an unbound closed attempt"):
            connection.execute(RETIREMENT, (PREDECESSOR, "a" * 64))
        connection.execute(
            "UPDATE work_main_creations SET state='closed' WHERE id=?", (PREDECESSOR,)
        )
        with pytest.raises(sqlite3.IntegrityError, match="unauthorized"):
            connection.execute(SUCCESSOR, successor)
        with pytest.raises(sqlite3.IntegrityError, match="requires an unbound retired attempt"):
            connection.execute(GRANT, ("grant", PREDECESSOR, "1" * 64))
        connection.execute(RETIREMENT, (PREDECESSOR, "a" * 64))
        connection.execute(GRANT, ("grant", PREDECESSOR, "1" * 64))
        for invalid in [
            (*successor[:2], "2" * 64, *successor[3:]),
            (*successor[:3], "unknown", *successor[4:]),
            (successor[0], "other-session", *successor[2:]),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match="unauthorized"):
                connection.execute(SUCCESSOR, invalid)
        connection.execute(SUCCESSOR, successor)
        assert execution_busy(connection)
        with pytest.raises(sqlite3.IntegrityError, match="main creation already occupied"):
            connection.execute("UPDATE runs SET status='queued' WHERE id='active'")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_existing_binding_disallows_successor_even_when_grant_is_present(original_attempt):
    original_attempt.initialize()
    with original_attempt.connect(write=True) as connection:
        connection.execute("UPDATE work_main_creations SET state='closed'")
        connection.execute(RETIREMENT, (PREDECESSOR, "a" * 64))
        connection.execute(GRANT, ("grant", PREDECESSOR, "1" * 64))
        connection.execute(
            "INSERT INTO work_runtime_bindings VALUES (?,'binding',?,100)", (SESSION, "2" * 64)
        )
        with pytest.raises(sqlite3.IntegrityError, match="unauthorized"):
            connection.execute(
                SUCCESSOR, ("child", SESSION, "1" * 64, "held", PREDECESSOR, "grant")
            )


def test_competing_grants_consume_only_one_successor_and_never_rewrite_history(original_attempt):
    original_attempt.initialize()
    with original_attempt.connect(write=True) as connection:
        connection.execute("UPDATE work_main_creations SET state='closed'")
        connection.execute(RETIREMENT, (PREDECESSOR, "a" * 64))

    barrier = Barrier(2, timeout=5)

    def acquire(number):
        identity = str(uuid4())
        digest = str(number) * 64
        barrier.wait()
        try:
            with original_attempt.connect(write=True) as connection:
                connection.execute(GRANT, (identity, PREDECESSOR, digest))
                connection.execute(
                    SUCCESSOR, (str(number), SESSION, digest, "held", PREDECESSOR, identity)
                )
        except sqlite3.IntegrityError as error:
            assert "requires an unbound retired attempt" in str(error)
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(acquire, (1, 2))) == [False, True]
    before = snapshot(original_attempt)
    with original_attempt.connect(write=True) as connection:
        rowid = connection.execute(
            "SELECT rowid FROM work_main_creations WHERE id=?", (PREDECESSOR,)
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="history or occupancy already exists"):
            connection.execute(
                "INSERT OR REPLACE INTO work_main_creations "
                "(rowid,id,session_id,choice_digest,owner_revision,workspace_revision,"
                "created_at,deadline_at,state) VALUES (?,'replacement','other-session',?,"
                "1,1,1,61,'closed')",
                (rowid, "4" * 64),
            )
        for statement, args in [
            (RETIREMENT.replace("INSERT", "INSERT OR REPLACE"), (PREDECESSOR, "3" * 64)),
            (GRANT.replace("INSERT", "INSERT OR REPLACE"), ("grant", PREDECESSOR, "3" * 64)),
            (
                SUCCESSOR.replace("INSERT", "INSERT OR REPLACE"),
                ("replacement", SESSION, "3" * 64, "closed", None, None),
            ),
            ("UPDATE work_main_creations SET rowid=100", ()),
            ("UPDATE work_main_attempt_retirements SET evidence_digest=?", ("3" * 64,)),
            ("UPDATE work_main_attempt_grants SET new_choice_digest=?", ("3" * 64,)),
            ("DELETE FROM work_main_attempt_retirements", ()),
            ("DELETE FROM work_main_attempt_grants", ()),
            ("UPDATE work_main_creations SET state='held' WHERE id=?", (PREDECESSOR,)),
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, args)
    assert snapshot(original_attempt) == before

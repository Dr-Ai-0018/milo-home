"""A real v26 choice projection survives the additive reception migrations intact."""

import sqlite3
from importlib.resources import files

from test_reply_choices import mixed_case

from milo_home.database import Database
from milo_home.reply_projection_evidence import read_projection


def test_v26_history_remains_identical_and_receivers_start_without_invented_choices(
    client, tmp_path
):
    _, store, run, _, _, result, _ = mixed_case(client)
    assert store.publish(run, result)
    path = tmp_path / "v26.sqlite3"
    store.database.backup(path)
    copy = Database(path)
    new_tables = {
        "reception_changes",
        "reception_slots",
        "reception_run_bases",
        "reception_returned_attempts",
    }
    with copy.connect(write=True) as connection:
        # Keep this fixture a real v26 schema as later additive features are introduced.
        connection.execute("DROP VIEW work_task_decisions")
        for table in (
            "work_window_facts",
            "work_result_receptions",
            "work_turn_bases",
            "work_choice_receipts",
            "work_revisit_claims",
            "work_decision_capabilities",
            "work_revisit_inputs",
            "work_continuations",
            "work_decisions",
            "work_revisits",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DROP TRIGGER message_decisions_scope_insert")
        connection.execute("DROP VIEW message_reply_state")
        connection.execute("DROP VIEW message_decision_history")
        for table in (
            "message_control_decisions",
            "revisit_controls",
            "revisit_claim_sources",
            "revisit_claims",
            "reply_revisit_capabilities",
        ):
            connection.execute(f"DROP TABLE {table}")
        statement = ""
        for line in (
            files("milo_home")
            .joinpath("migrations", "026_reply_choices.sql")
            .read_text()
            .splitlines(keepends=True)
        ):
            statement += line
            if sqlite3.complete_statement(statement):
                if statement.lstrip().startswith(
                    (
                        "CREATE VIEW message_reply_state",
                        "CREATE TRIGGER message_decisions_scope_insert",
                    )
                ):
                    connection.execute(statement)
                statement = ""
        # Remove only the synthetic copy's new empty reception feature and launch baseline.
        for table in (
            "reception_returned_attempts",
            "reception_run_bases",
            "reception_slots",
            "reception_changes",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=26")
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
    copy.initialize()
    with copy.snapshot() as connection:
        for table, rows in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == rows
        for table in new_tables:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert read_projection(connection, run)["decisions"]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

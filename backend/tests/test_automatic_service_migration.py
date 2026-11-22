from importlib.resources import files
from uuid import uuid4

from milo_home.backup_rehearsal import rehearse_backup
from milo_home.database import CURRENT_SCHEMA_VERSION, MIGRATIONS, Database


def test_v24_copy_migration_preserves_old_rows_and_creates_no_enabled_entry(
    private_runtime_directory,
):
    directory = private_runtime_directory("milo-service-migration-")
    source = directory / "v24.sqlite3"
    source.touch(mode=0o600)
    database = Database(source)
    with database.connect() as connection:
        for number, name in MIGRATIONS:
            if number <= 24:
                connection.executescript(
                    files("milo_home").joinpath("migrations", name).read_text()
                )
        connection.execute("PRAGMA user_version=24")
        owner, thread = str(uuid4()), str(uuid4())
        connection.execute(
            "INSERT INTO users VALUES (?, 'old', '合成旧账号', 'unused', 1)", (owner,)
        )
        connection.execute("INSERT INTO threads VALUES (?,?,1)", (thread, owner))
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for (table,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    report = rehearse_backup(source, directory / "restored.sqlite3")
    assert (
        report["copied_schema_version"] == 24 and report["schema_version"] == CURRENT_SCHEMA_VERSION
    )
    assert report["record_counts"]["automatic_services"] == 0
    with Database(directory / "restored.sqlite3").snapshot() as connection:
        for table, rows in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == rows
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with database.snapshot() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 24

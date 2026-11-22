"""Local public-content lifecycle, explicit publication and inert legacy reimports."""

import argparse
import json
import sqlite3
import sys
from uuid import uuid4

import pytest

from milo_home.cli import main
from milo_home.database import CURRENT_SCHEMA_VERSION, Database
from milo_home.public_notes_cli import add_commands
from milo_home.public_notes_legacy import LEGACY_NOTES, legacy_id


@pytest.fixture
def note_cli(tmp_path, monkeypatch, capsys):
    tmp_path.chmod(0o700)
    database = Database(tmp_path / "home.sqlite3")
    database.initialize()
    monkeypatch.setenv("MILO_DATABASE", str(database.path))

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["milo-home", "content", "note", *map(str, args)])
        main()
        return json.loads(capsys.readouterr().out)

    return database, run


def note_file(tmp_path, **changes):
    value = {
        "title": "公开的小事",
        "summary": "明确选择的公开摘要。",
        "body": "第一段。\n\n第二段。",
        "category": "小家",
        "content_date": "2026-11-22",
    }
    value.update(changes)
    path = tmp_path / "note.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_create_revise_publish_withdraw_cycle(note_cli, tmp_path):
    _, run = note_cli
    note_id = str(uuid4())
    source = note_file(tmp_path)
    draft = run("create", "--id", note_id, "--file", source)
    assert draft["published"] is None
    assert draft["latest"]["body"] == "第一段。\n\n第二段。"
    repeated = run("create", "--id", note_id, "--file", source)
    assert repeated == draft
    public = run(
        "publish", "--id", note_id, "--expected-version", draft["version"], "--confirm-public"
    )
    assert public["published"] == public["latest"]
    unchanged = run(
        "publish", "--id", note_id, "--expected-version", public["version"], "--confirm-public"
    )
    assert unchanged == public
    note_file(tmp_path, title="修订后的公开小事")
    revised = run(
        "revise", "--id", note_id, "--expected-version", public["version"], "--file", source
    )
    assert revised["latest"]["title"] == "修订后的公开小事"
    assert revised["published"]["title"] == "公开的小事"
    withdrawn = run("withdraw", "--id", note_id, "--expected-version", revised["version"])
    assert withdrawn["published"] is None
    assert withdrawn["latest"] == revised["latest"]
    assert run("show", "--id", note_id) == withdrawn
    republished = run(
        "publish", "--id", note_id, "--expected-version", withdrawn["version"], "--confirm-public"
    )
    assert republished["sequence"] == public["sequence"]
    assert republished["published"] == revised["latest"]


def test_stale_publication_cannot_revive_withdrawn_note(note_cli, tmp_path, capsys):
    _, run = note_cli
    note_id = str(uuid4())
    draft = run("create", "--id", note_id, "--file", note_file(tmp_path))
    public = run(
        "publish", "--id", note_id, "--expected-version", draft["version"], "--confirm-public"
    )
    withdrawn = run("withdraw", "--id", note_id, "--expected-version", public["version"])
    with pytest.raises(SystemExit) as error:
        run("publish", "--id", note_id, "--expected-version", public["version"], "--confirm-public")
    assert error.value.code == 1
    capsys.readouterr()
    assert run("show", "--id", note_id) == withdrawn


def test_invalid_input_is_not_echoed_or_created(note_cli, tmp_path, capsys):
    _, run = note_cli
    note_id = str(uuid4())
    source = note_file(tmp_path, private_value="synthetic-private-content")
    with pytest.raises(SystemExit) as error:
        run("create", "--id", note_id, "--file", source)
    assert error.value.code == 1
    output = capsys.readouterr()
    assert not output.out and "synthetic-private-content" not in output.err
    with pytest.raises(SystemExit):
        run("show", "--id", note_id)


def test_legacy_import_preserves_text_order_and_does_not_restore_changes(note_cli, tmp_path):
    _, run = note_cli
    imported = run("import-legacy", "--confirm-public")
    expected = [legacy_id(note["key"]) for note in reversed(LEGACY_NOTES)]
    assert imported["created"] == expected and imported["skipped"] == []
    original = [run("show", "--id", legacy_id(note["key"])) for note in LEGACY_NOTES]
    assert original[0]["sequence"] > original[1]["sequence"] > original[2]["sequence"]
    for state, note in zip(original, LEGACY_NOTES, strict=True):
        assert state["source_key"] == "legacy-home:" + note["key"]
        assert state["published"] == {
            "title": note["title"],
            "summary": note["text"],
            "body": note["text"],
            "category": note["category"],
            "content_date": "2026-11-14",
        }
    first = original[0]
    revised = run(
        "revise",
        "--id",
        first["id"],
        "--expected-version",
        first["version"],
        "--file",
        note_file(tmp_path, title="明确修订的旧近况"),
    )
    withdrawn = run("withdraw", "--id", first["id"], "--expected-version", revised["version"])
    repeated = run("import-legacy", "--confirm-public")
    assert repeated["created"] == [] and repeated["skipped"] == expected
    assert run("show", "--id", first["id"]) == withdrawn


def test_legacy_import_failure_rolls_back_before_retry(note_cli, monkeypatch, capsys):
    from milo_home import public_notes

    database, run = note_cli
    append_revision = public_notes.append_revision

    def interrupt(*args, **kwargs):
        raise ValueError("Synthetic interruption after inserting the note")

    monkeypatch.setattr(public_notes, "append_revision", interrupt)
    with pytest.raises(SystemExit) as error:
        run("import-legacy", "--confirm-public")
    assert error.value.code == 1
    capsys.readouterr()
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM public_notes").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM public_note_revisions").fetchone()[0] == 0
    monkeypatch.setattr(public_notes, "append_revision", append_revision)
    imported = run("import-legacy", "--confirm-public")
    assert len(imported["created"]) == 3 and imported["skipped"] == []
    for note in LEGACY_NOTES:
        assert run("show", "--id", legacy_id(note["key"]))["published"] is not None


@pytest.mark.parametrize("action", ["publish", "import-legacy"])
def test_public_confirmation_and_help_never_open_database(action, monkeypatch):
    parser = argparse.ArgumentParser()
    add_commands(parser.add_subparsers(dest="command", required=True))
    monkeypatch.setattr(
        Database, "connect", lambda *a, **kw: pytest.fail("Unexpected database read")
    )
    arguments = ["content", "note", action]
    if action == "publish":
        arguments += ["--id", str(uuid4()), "--expected-version", "1"]
    with pytest.raises(SystemExit) as error:
        parser.parse_args(arguments)
    assert error.value.code == 2
    with pytest.raises(SystemExit) as help_:
        parser.parse_args(["content", "note", "--help"])
    assert help_.value.code == 0


@pytest.mark.parametrize("version", ["0", "-1", "x"])
def test_expected_version_requires_positive_integer(version):
    parser = argparse.ArgumentParser()
    add_commands(parser.add_subparsers(dest="command", required=True))
    with pytest.raises(SystemExit) as error:
        parser.parse_args(
            ["content", "note", "withdraw", "--id", str(uuid4()), "--expected-version", version]
        )
    assert error.value.code == 2


@pytest.mark.parametrize("exists", [False, True])
def test_command_neither_initializes_nor_migrates_database(tmp_path, monkeypatch, exists):
    path = tmp_path / "uninitialized.sqlite3"
    if exists:
        with sqlite3.connect(path) as connection:
            connection.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION - 1}")
    monkeypatch.setenv("MILO_DATABASE", str(path))
    monkeypatch.setattr(
        sys, "argv", ["milo-home", "content", "note", "import-legacy", "--confirm-public"]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert path.exists() is exists
    if exists:
        with sqlite3.connect(path) as connection:
            assert (
                connection.execute("PRAGMA user_version").fetchone()[0]
                == CURRENT_SCHEMA_VERSION - 1
            )
            assert connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0

"""The real content work entry, including inert help and service-owned sealing."""

import json
import sys
from uuid import uuid4

import pytest
from test_public_works import content

from milo_home.cli import main
from milo_home.database import Database
from milo_home.public_works import PublicWorks


@pytest.fixture
def work_cli(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    assets = tmp_path / "assets"
    assets.mkdir(mode=0o700)
    database = Database(data / "home.sqlite3")
    database.initialize()
    monkeypatch.setenv("MILO_DATABASE", str(database.path))
    monkeypatch.setenv("MILO_PUBLIC_ASSET_ROOT", str(assets))
    monkeypatch.setenv("MILO_PUBLIC_ASSET_MAX_BYTES", "268435456")

    def run(*arguments):
        monkeypatch.setattr(sys, "argv", ["milo-home", "content", "work", *map(str, arguments)])
        main()
        return json.loads(capsys.readouterr().out)

    return database, run


def test_real_cli_create_import_revise_publish_withdraw(work_cli, tmp_path):
    database, run = work_cli
    identity, asset_id = str(uuid4()), str(uuid4())
    input_ = tmp_path / "work.json"
    input_.write_text(json.dumps(content()), encoding="utf-8")
    draft = run("create", "--id", identity, "--file", input_)
    assert draft["version"] == 1 and draft["published"] is None
    source = tmp_path / "说明.pdf"
    source.write_bytes(b"synthetic-public-file")
    asset = run("asset", "import", "--id", identity, "--asset-id", asset_id, "--file", source)
    assert asset["filename"] == "说明.pdf"
    input_.write_text(json.dumps(content(links=[], files=[asset_id])), encoding="utf-8")
    revised = run("revise", "--id", identity, "--file", input_, "--expected-version", 1)
    source.unlink()
    public = run(
        "publish", "--id", identity, "--expected-version", revised["version"], "--confirm-public"
    )
    assert PublicWorks(database).get(identity)["files"][0]["sha256"] == asset["sha256"]
    assert public["published_revision"] == 2
    assert run("show", "--id", identity) == public
    withdrawn = run("withdraw", "--id", identity, "--expected-version", public["version"])
    assert withdrawn["published"] is None


def test_missing_public_confirmation_and_help_do_not_open_or_create_data(monkeypatch, tmp_path):
    monkeypatch.setenv("MILO_DATABASE", str(tmp_path / "absent.sqlite3"))
    monkeypatch.setattr(Database, "connect", lambda *a, **kw: pytest.fail("Unexpected data access"))
    for arguments, code in (
        (["--help"], 0),
        (["asset", "import", "--help"], 0),
        (["publish", "--id", str(uuid4()), "--expected-version", "1"], 2),
    ):
        monkeypatch.setattr(sys, "argv", ["milo-home", "content", "work", *arguments])
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == code
    assert not (tmp_path / "absent.sqlite3").exists()


def test_cli_does_not_initialize_old_or_missing_database(tmp_path, monkeypatch, capsys):
    import sqlite3

    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    for exists in (False, True):
        path = data / f"old-{exists}.sqlite3"
        if exists:
            with sqlite3.connect(path) as connection:
                connection.execute("PRAGMA user_version=34")
        monkeypatch.setenv("MILO_DATABASE", str(path))
        monkeypatch.setattr(
            sys, "argv", ["milo-home", "content", "work", "show", "--id", str(uuid4())]
        )
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 1
        capsys.readouterr()
        assert path.exists() is exists
        if exists:
            with sqlite3.connect(path) as connection:
                assert connection.execute("PRAGMA user_version").fetchone()[0] == 34

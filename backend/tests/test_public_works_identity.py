"""Real management identity: source read as root, files and WAL owned by service."""

import json
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from test_database_identity import owned_database as owned_database
from test_public_works import content

from milo_home.cli import main


@pytest.mark.skipif(os.geteuid() != 0, reason="Real service identity requires root")
def test_cli_service_account_keeps_sealed_files_and_wal_owned(
    owned_database, tmp_path, monkeypatch, capsys
):
    database, account = owned_database
    assets = Path(tempfile.mkdtemp(prefix="milo-public-assets-identity-"))
    os.chown(assets, account.pw_uid, account.pw_gid)
    monkeypatch.setenv("MILO_DATABASE", str(database))
    monkeypatch.setenv("MILO_PUBLIC_ASSET_ROOT", str(assets))
    monkeypatch.setenv("MILO_PUBLIC_ASSET_MAX_BYTES", "268435456")

    def run(*args):
        monkeypatch.setattr(
            sys,
            "argv",
            ["milo-home", "--database-user", account.pw_name, "content", "work", *map(str, args)],
        )
        main()
        assert os.geteuid() == 0
        return json.loads(capsys.readouterr().out)

    identity, asset_id = str(uuid4()), str(uuid4())
    input_ = tmp_path / "work.json"
    input_.write_text(json.dumps(content()), encoding="utf-8")
    draft = run("create", "--id", identity, "--file", input_)
    source = tmp_path / "explicit.bin"
    source.write_bytes(b"synthetic-source-in-root-private-directory")
    run("asset", "import", "--id", identity, "--asset-id", asset_id, "--file", source)
    input_.write_text(json.dumps(content(files=[asset_id])), encoding="utf-8")
    revised = run(
        "revise", "--id", identity, "--file", input_, "--expected-version", draft["version"]
    )
    source.unlink()
    public = run(
        "publish", "--id", identity, "--expected-version", revised["version"], "--confirm-public"
    )
    assert public["published_revision"] == 2
    for entry in (
        assets / "works",
        assets / "works" / identity,
        assets / "works" / identity / asset_id,
        *database.parent.iterdir(),
    ):
        assert entry.stat().st_uid == account.pw_uid
    assert (assets / "works" / identity / asset_id).stat().st_mode & 0o777 == 0o400

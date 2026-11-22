"""Controlled registration makes native revisit support explicit and refuses a legacy fallback."""

import json
import sys

import pytest
from test_reply_readiness import preparation as preparation

from milo_home.cli import main
from milo_home.reply_execution import run_once
from milo_home.scheduler_cli import StopRequest


def test_cli_registration_records_revisit_capability_and_legacy_receiver_refuses(
    preparation, monkeypatch, capsys
):
    database, owner, _, _, directory, profile, credential = preparation
    monkeypatch.setenv("MILO_DATABASE", str(database.path))
    base = [
        "milo-home",
        "--run-directory",
        str(directory),
        "automatic-service",
        "register",
        "--kind",
        "reply",
        "--owner",
        owner,
        "--timer-unit",
        "milo-home-reply.timer",
        "--enable",
    ]
    monkeypatch.setattr(sys, "argv", [*base, "--revisits"])
    main()
    assert json.loads(capsys.readouterr().out)["revisits_enabled"] is True
    monkeypatch.setattr(sys, "argv", base)
    main()
    assert json.loads(capsys.readouterr().out)["revisits_enabled"] is True
    with pytest.raises(ValueError, match="requires the configured native"):
        run_once(database, directory, owner, profile, credential, 3600, StopRequest())
    with database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT fault_code FROM automatic_services WHERE kind='reply'"
            ).fetchone()[0]
            == "configuration_error"
        )

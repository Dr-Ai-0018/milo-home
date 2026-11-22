import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_execution_control import execution_case as execution_case
from test_work_initialization import init_case as init_case
from test_work_process import fake_units as fake_units
from test_work_setup import invoke
from test_work_tasks import work_case as work_case

from milo_home.codex_config import CodexRuntime
from milo_home.codex_journal import PrivateJournal
from milo_home.codex_rpc import ProtocolError
from milo_home.execution_control import execution_busy
from milo_home.work_creation_records import CHOICE, CreationRecords

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root creation")


@pytest.fixture
def creation_case(init_case, fake_units, monkeypatch, tmp_path):
    monkeypatch.setattr("milo_home.work_creation_process.DirectUnit", fake_units[0])
    output = tmp_path / "creation-requests.ndjson"
    selected = ["normal"]
    peer = Path(__file__).parent / "fixtures/codex_main_creation.py"
    monkeypatch.setattr(
        CodexRuntime,
        "command",
        lambda _, *, workspace: [sys.executable, str(peer), selected[0], str(output)],
    )
    monkeypatch.setenv("SYNTHETIC_KEY", "synthetic-secret")
    stops = []
    monkeypatch.setattr(
        "milo_home.work_creation.stop_family", lambda identity: stops.append(identity) or True
    )
    return init_case, fake_units[1], output, selected, stops


def call(case, monkeypatch, *, recover=False, database=None, account=None, timeout=60):
    initial = case[0]
    work = initial[0][0]
    options = ["--owner", work[1], "--session", work[-1]["id"], "--timeout", str(timeout)]
    if recover:
        action = "recover-main"
        options += ["--confirm-stop"]
    else:
        action = "create-main"
        options += [
            "--cli",
            str(initial[3].cli),
            "--tool-host",
            str(initial[3].tool_host),
            "--create",
        ]
    invoke(monkeypatch, database or work[0], initial[1], action, *options, account=account)


def requests(case):
    return (
        [json.loads(line) for line in case[2].read_text().splitlines()] if case[2].exists() else []
    )


def state(case):
    with case[0][0][0][0].connect() as connection:
        row = connection.execute("SELECT state FROM work_main_creations").fetchone()
        return (row[0] if row else None), execution_busy(connection)


def test_cli_creates_seeds_stops_resumes_original_and_registers(creation_case, monkeypatch, capsys):
    call(creation_case, monkeypatch)
    report = json.loads(capsys.readouterr().out)
    assert report["protocol_verified"] and report["persistence_verified"]
    assert not report["execution_available"] and state(creation_case) == ("closed", False)
    methods = [r["method"] for r in requests(creation_case)]
    assert methods == [
        "initialize",
        "initialized",
        "thread/start",
        "thread/inject_items",
        "initialize",
        "initialized",
        "thread/resume",
    ]
    journal = PrivateJournal(creation_case[0][1], initialize=False)
    assert journal.binding().thread_id == "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    assert journal.business_binding().session_id == creation_case[0][0][0][-1]["id"]
    assert len(creation_case[1]) == 2 and all(
        u.process.poll() is not None for u in creation_case[1]
    )
    assert not list(creation_case[0][1].glob("*.env"))
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    assert [r["method"] for r in requests(creation_case)] == methods


@pytest.mark.parametrize("scenario", ["create-eof", "marker-eof", "bad-uuid", "wrong-provider"])
def test_unknown_or_invalid_create_never_repeats_or_registers(creation_case, monkeypatch, scenario):
    creation_case[3][0] = scenario
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    assert state(creation_case) == ("unknown", True)
    before = requests(creation_case)
    creation_case[3][0] = "normal"
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch, recover=True)
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    assert requests(creation_case) == before
    assert not (creation_case[0][1] / "session-journal.sqlite3").exists()
    assert creation_case[4]


def test_known_create_and_marker_resume_can_be_retried_only_as_metadata_read(
    creation_case, monkeypatch, capsys
):
    creation_case[3][0] = "resume-eof"
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    assert state(creation_case) == ("unknown", True)
    choice = creation_case[0][1] / CHOICE
    original = choice.read_bytes()
    assert json.loads(original)["version"] == "codex-cli 0.167.0"
    monkeypatch.setattr(CodexRuntime, "verify", lambda _: "codex-cli 1.0.0")
    creation_case[3][0] = "normal"
    call(creation_case, monkeypatch, recover=True)
    assert choice.read_bytes() == original
    assert json.loads(capsys.readouterr().out)["association_recorded"]
    methods = [r["method"] for r in requests(creation_case)]
    assert methods.count("thread/start") == methods.count("thread/inject_items") == 1
    assert methods.count("thread/resume") == 2
    assert state(creation_case) == ("closed", False) and len(creation_case[4]) == 2


def test_partial_business_commit_recovers_without_model_or_credentials(
    creation_case, monkeypatch, capsys
):
    original = PrivateJournal.bind_business

    def fail(*args):
        raise OSError("synthetic business-first partial commit")

    monkeypatch.setattr(PrivateJournal, "bind_business", fail)
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    assert state(creation_case) == ("closed", False)
    before = requests(creation_case)
    monkeypatch.setattr(PrivateJournal, "bind_business", original)
    monkeypatch.delenv("SYNTHETIC_KEY")
    call(creation_case, monkeypatch, recover=True)
    assert json.loads(capsys.readouterr().out)["association_recorded"]
    assert requests(creation_case) == before and len(creation_case[4]) == 2


def test_missing_or_changed_original_process_identity_stays_occupied(creation_case, monkeypatch):
    creation_case[3][0] = "marker-eof"
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    monkeypatch.setattr("milo_home.work_creation.stop_family", lambda _: False)
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch, recover=True)
    assert state(creation_case) == ("unknown", True)
    assert len(creation_case[1]) == 1


def test_response_record_failure_is_unknown_even_if_peer_returned_it(creation_case, monkeypatch):
    original = CreationRecords.record

    def fail(self, event):
        if event.get("kind") == "response" and event.get("id") == 2:
            raise OSError("synthetic response publication failure")
        return original(self, event)

    monkeypatch.setattr(CreationRecords, "record", fail)
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    assert state(creation_case) == ("unknown", True)
    monkeypatch.setattr(CreationRecords, "record", original)
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch, recover=True)
    assert [r["method"] for r in requests(creation_case)].count("thread/start") == 1


def test_missing_creation_choice_cannot_be_replaced_by_offline_registration(
    creation_case, monkeypatch
):
    from test_work_initialization import initialize

    creation_case[3][0] = "create-eof"
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    path = creation_case[0][1] / CHOICE
    path.rename(path.with_name("preserved-creation.json"))
    with pytest.raises(ProtocolError):
        initialize(creation_case[0])
    assert state(creation_case) == ("unknown", True)


@pytest.mark.parametrize("change", ["home", "cli", "tool_host", "config"])
def test_closed_creation_cannot_change_original_environment_before_registration(
    creation_case, monkeypatch, change, private_runtime_directory
):
    from test_work_initialization import initialize

    import milo_home.work_creation as module

    original = module.register_main_locked

    def fail(*args, **kwargs):
        raise OSError("synthetic stop before registration intent")

    monkeypatch.setattr(module, "register_main_locked", fail)
    with pytest.raises(SystemExit):
        call(creation_case, monkeypatch)
    assert state(creation_case) == ("closed", False)
    monkeypatch.setattr(module, "register_main_locked", original)
    initial = creation_case[0]
    directory = initial[1]
    kwargs = {"thread_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}
    if change == "home":
        directory = private_runtime_directory("milo-creation-copy-")
        shutil.copytree(initial[1], directory, dirs_exist_ok=True)
        kwargs["directory"] = directory
    elif change == "config":
        (directory / "config.toml").write_bytes(initial[4].content + b"# altered\n")
    else:
        kwargs["installed"] = replace(initial[3], **{change: Path("/synthetic/other")})
    with pytest.raises(ProtocolError):
        initialize(initial, **kwargs)
    assert not (directory / "main-registration.json").exists()
    if change == "config":
        (directory / "config.toml").write_bytes(initial[4].content)
    before = requests(creation_case)
    call(creation_case, monkeypatch, recover=True)
    assert requests(creation_case) == before

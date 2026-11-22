import hashlib
import json
import os
from uuid import uuid4

import pytest
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_cli import runtime_case as runtime_case
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_runtime import SYNTHETIC_CONFIG, bind
from test_work_setup import invoke
from test_work_tasks import create
from test_work_tasks import work_case as work_case

from milo_home.codex_config import CodexRuntime, NewawzConfig, private_create
from milo_home.codex_execution_policy import NativeExecutionPolicy
from milo_home.codex_rpc import ProtocolError
from milo_home.execution_lock import execution_lock
from milo_home.native_policy import NativePolicy, NativePolicyStore
from milo_home.work_process import WorkLauncher
from milo_home.work_runtime import WorkRuntime

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Private host registration requires root")


@pytest.fixture
def native_case(runtime_case, monkeypatch):
    case, runtime, journal = runtime_case
    with execution_lock(runtime.database) as lease:
        bind(runtime_case, lease)
    private_create(journal.path.parent / "AGENTS.md", b"Synthetic native home rules.\n")
    config = NewawzConfig(
        "synthetic-provider", "synthetic-model", "SYNTHETIC_KEY", SYNTHETIC_CONFIG
    )
    monkeypatch.setattr(NewawzConfig, "load", lambda: config)
    monkeypatch.setattr(CodexRuntime, "verify", lambda self: None)
    monkeypatch.setattr(
        "milo_home.native_policy_cli.WorkRuntime",
        lambda database, journal: WorkRuntime(database, journal, runtime.clock),
    )
    return runtime_case


def call(native_case, monkeypatch, capsys, *arguments, owner=None):
    case, runtime, journal = native_case
    invoke(
        monkeypatch,
        runtime.database,
        journal.path.parent,
        "native-policy",
        "--owner",
        owner or case[0][1],
        "--session",
        case[0][-1]["id"],
        *arguments,
    )
    return json.loads(capsys.readouterr().out)


def configure(native_case, monkeypatch, capsys, *, expected="none", model="gpt-6.2-sol"):
    return call(
        native_case,
        monkeypatch,
        capsys,
        "--configure",
        "--model",
        model,
        "--expected",
        expected,
    )


def test_inspection_creates_no_policy_or_execution(native_case, monkeypatch, capsys):
    _, runtime, journal = native_case
    before = set(journal.path.parent.iterdir())
    assert call(native_case, monkeypatch, capsys) == {
        "registered": False,
        "execution_available": False,
    }
    assert set(journal.path.parent.iterdir()) == before
    assert runtime.execution_policy is NativeExecutionPolicy.LEGACY_DIRECT


def test_registration_keeps_native_identity_config_and_business_history(
    native_case, monkeypatch, capsys
):
    case, runtime, journal = native_case
    before = journal.binding(), journal.business_binding(), journal.fingerprint()
    original_config = (journal.path.parent / "config.toml").read_bytes()
    result = configure(native_case, monkeypatch, capsys)
    assert result["revision"] == 1
    assert result["model"] == "gpt-6.2-sol"
    assert result["execution_policy"] == "auto_review"
    assert result["execution_available"] is False
    assert before == (journal.binding(), journal.business_binding(), journal.fingerprint())
    assert (journal.path.parent / "config.toml").read_bytes() == original_config
    reopened = WorkRuntime(runtime.database, journal, runtime.clock)
    assert reopened.conversation_binding().thread_id == before[0].thread_id
    assert reopened.conversation_binding().model == "gpt-6.2-sol"
    assert reopened.execution_policy is NativeExecutionPolicy.AUTO_REVIEW
    with execution_lock(runtime.database) as lease:
        reopened.check_session_binding(case[0][1], case[0][-1]["id"], lease=lease)
        with pytest.raises(ProtocolError, match="policy changed"):
            runtime.check_session_binding(case[0][1], case[0][-1]["id"], lease=lease)
    with runtime.database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM execution_windows").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM execution_permits").fetchone()[0] == 0


def test_update_requires_current_revision_and_keeps_old_policy(native_case, monkeypatch, capsys):
    first = configure(native_case, monkeypatch, capsys)
    assert configure(native_case, monkeypatch, capsys) == first
    second = configure(
        native_case, monkeypatch, capsys, expected=first["digest"], model="another-supported-model"
    )
    assert second["revision"] == 2 and second["digest"] != first["digest"]
    with pytest.raises(SystemExit):
        configure(native_case, monkeypatch, capsys, expected=first["digest"], model="stale-choice")
    capsys.readouterr()
    assert call(native_case, monkeypatch, capsys) == second
    store = NativePolicyStore(native_case[2])
    with store._connect() as connection:
        assert connection.execute("SELECT count(*) FROM revisions").fetchone()[0] == 2
        old = connection.execute("SELECT digest FROM revisions WHERE revision=1").fetchone()[0]
        assert old == first["digest"]


@pytest.mark.parametrize(
    "reason", ["busy", "unknown", "wrong-owner", "bad-model", "missing-expected"]
)
def test_invalid_or_unsettled_registration_never_enables_policy(
    native_case, monkeypatch, capsys, reason
):
    case, runtime, journal = native_case
    store = NativePolicyStore(journal)
    if reason == "busy":
        reserve(case)
    elif reason == "unknown":
        journal.open_stream("milo-work-" + uuid4().hex + ".service")
    arguments = ["--configure", "--model", "has whitespace" if reason == "bad-model" else "valid"]
    if reason != "missing-expected":
        arguments += ["--expected", "none"]
    with pytest.raises(SystemExit):
        call(
            native_case,
            monkeypatch,
            capsys,
            *arguments,
            owner=str(uuid4()) if reason == "wrong-owner" else None,
        )
    capsys.readouterr()
    assert not store.path.exists() and not store.required.exists()


@pytest.mark.parametrize("damage", ["missing", "linked", "public", "marker-missing"])
def test_registered_policy_damage_cannot_fall_back_to_never(
    native_case, monkeypatch, capsys, damage
):
    configure(native_case, monkeypatch, capsys)
    _, runtime, journal = native_case
    store = NativePolicyStore(journal)
    if damage == "missing":
        store.path.unlink()
    elif damage == "linked":
        target = store.path.with_suffix(".saved")
        store.path.rename(target)
        store.path.symlink_to(target)
    elif damage == "public":
        store.path.chmod(0o644)
    else:
        store.required.unlink()
    with pytest.raises((OSError, ValueError, ProtocolError)):
        WorkRuntime(runtime.database, journal, runtime.clock)


def test_configured_work_process_resumes_original_thread_with_new_native_policy(
    launch_case, fake_units
):
    case, runtime, journal, original_launcher, output, scenario = launch_case
    private_create(journal.path.parent / "AGENTS.md", b"Synthetic native home rules.\n")
    with execution_lock(runtime.database):
        NativePolicyStore(journal).configure(
            owner=case[0][1],
            session=case[0][-1]["id"],
            model="gpt-6.2-sol",
            expected=None,
            now=int(runtime.clock()),
        )
    runtime = WorkRuntime(runtime.database, journal, runtime.clock)
    launcher = WorkLauncher(runtime, original_launcher.config, original_launcher.installed)
    scenario[0] = "auto-normal"
    _, window = reserve(case)
    with launcher.open(case[0][1], window["id"], credential="synthetic-secret") as conversation:
        assert conversation.binding.thread_id == journal.binding().thread_id
        assert conversation.binding.model == "gpt-6.2-sol"
        assert conversation.state == "idle"
    requests = [json.loads(line) for line in output.read_text().splitlines()]
    resumed = next(r["params"] for r in requests if r["method"] == "thread/resume")
    assert resumed["approvalsReviewer"] == "auto_review"
    assert resumed["model"] == "gpt-6.2-sol"
    assert not any(r["method"] == "turn/start" for r in requests)
    assert not journal.outstanding()
    assert not list(journal.path.parent.glob("*.env"))


def test_configured_task_uses_native_turn_and_publishes_without_replacing_main(
    launch_case, fake_units
):
    case, runtime, journal, original_launcher, output, scenario = launch_case
    private_create(journal.path.parent / "AGENTS.md", b"Synthetic native home rules.\n")
    with execution_lock(runtime.database):
        NativePolicyStore(journal).configure(
            owner=case[0][1],
            session=case[0][-1]["id"],
            model="gpt-6.2-sol",
            expected=None,
            now=int(runtime.clock()),
        )
    runtime = WorkRuntime(runtime.database, journal, runtime.clock)
    launcher = WorkLauncher(runtime, original_launcher.config, original_launcher.installed)
    scenario[0] = "auto-normal"
    task = create(case[0])
    _, result = launcher.run_task(case[0][1], task["id"], 30, credential="synthetic-secret")
    assert result.outcome == "completed" and result.turns == 1
    requests = [json.loads(line) for line in output.read_text().splitlines()]
    assert sum(r["method"] == "turn/start" for r in requests) == 1
    assert not any(r["method"] == "thread/start" for r in requests)
    assert not journal.outstanding()
    with runtime.database.snapshot() as connection:
        assert (
            connection.execute(
                "SELECT status FROM work_tasks WHERE id=?", (task["id"],)
            ).fetchone()[0]
            == "completed"
        )


@pytest.mark.parametrize("damage", ["missing", "public", "linked", "empty", "invalid", "override"])
def test_invalid_native_home_rules_do_not_register_policy(native_case, monkeypatch, capsys, damage):
    journal = native_case[2]
    path = journal.path.parent / "AGENTS.md"
    if damage == "missing":
        path.unlink()
    elif damage == "public":
        path.chmod(0o644)
    elif damage == "linked":
        path.rename(path.with_suffix(".saved"))
        path.symlink_to(path.with_suffix(".saved"))
    elif damage == "empty":
        path.write_bytes(b" \n")
    elif damage == "invalid":
        path.write_bytes(b"\xff")
    else:
        private_create(path.with_name("AGENTS.override.md"), b"Unregistered alternative rules")
    with pytest.raises(SystemExit):
        configure(native_case, monkeypatch, capsys)
    capsys.readouterr()
    assert not NativePolicyStore(journal).required.exists()


def test_instruction_revision_requires_explicit_registration(native_case, monkeypatch, capsys):
    first = configure(native_case, monkeypatch, capsys)
    _, runtime, journal = native_case
    runtime = WorkRuntime(runtime.database, journal, runtime.clock)
    runtime.verify_execution_rules()
    (journal.path.parent / "AGENTS.md").write_bytes(b"Changed synthetic native rules.\n")
    with pytest.raises(ProtocolError, match="instructions changed"):
        runtime.verify_execution_rules()
    # Inspection/reconciliation remains available, but cannot authorize execution.
    assert call(native_case, monkeypatch, capsys) == first
    second = configure(native_case, monkeypatch, capsys, expected=first["digest"])
    assert second["revision"] == 2 and second["digest"] != first["digest"]
    WorkRuntime(runtime.database, journal, runtime.clock).verify_execution_rules()


def test_old_policy_canonical_bytes_survive_but_cannot_execute_without_pinned_rules(
    native_case, monkeypatch, capsys
):
    configure(native_case, monkeypatch, capsys)
    store = NativePolicyStore(native_case[2])
    old = store.read().model_copy(update={"instructions_digest": None})
    encoded = old.encoded()
    assert "instructions_digest" not in json.loads(encoded)
    assert NativePolicy.model_validate_json(encoded).encoded() == encoded
    assert hashlib.sha256(encoded.encode()).hexdigest() == old.digest
    with pytest.raises(ProtocolError, match="explicitly registered"):
        store.verify_instructions(old)


def test_changed_rules_reject_launch_before_occupancy_or_turn(native_case, monkeypatch, capsys):
    configure(native_case, monkeypatch, capsys)
    _, runtime, journal = native_case
    runtime = WorkRuntime(runtime.database, journal, runtime.clock)
    (journal.path.parent / "AGENTS.md").write_bytes(b"Unregistered changed rules.\n")
    launcher = WorkLauncher(runtime, None, None)
    with pytest.raises(ProtocolError, match="instructions changed"):
        launcher._prepare("synthetic-secret", execute=True)
    with runtime.database.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM execution_windows").fetchone()[0] == 0
    assert not journal.outstanding()

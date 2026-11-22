import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from test_database_identity import owned_database as owned_database
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_runtime import SYNTHETIC_CONFIG, bind
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case

from milo_home.codex_config import CodexRuntime, NewawzConfig
from milo_home.codex_process import DirectUnit
from milo_home.codex_rpc import ProtocolError
from milo_home.database_identity import ServiceDatabase
from milo_home.errors import ApiError
from milo_home.execution_lock import execution_lock
from milo_home.work_process import WorkLauncher

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Host lease requires root")


@pytest.fixture
def launch_case(runtime_case, monkeypatch, tmp_path):
    case, runtime, journal = runtime_case
    installed = CodexRuntime(Path("/synthetic/codex"), Path("/synthetic/host"))
    config = NewawzConfig(
        "synthetic-provider", "synthetic-model", "SYNTHETIC_KEY", SYNTHETIC_CONFIG
    )
    output = tmp_path / "requests.ndjson"
    scenario = ["normal"]
    peer = Path(__file__).parent / "fixtures/codex_app_server.py"
    monkeypatch.setattr(
        CodexRuntime,
        "command",
        lambda _, *, workspace: [sys.executable, str(peer), scenario[0], str(output)],
    )
    monkeypatch.setattr("milo_home.codex_process.time.time", lambda: case[-1][0])
    with execution_lock(case[0][0]) as lease:
        bind(runtime_case, lease)
    return case, runtime, journal, WorkLauncher(runtime, config, installed), output, scenario


@pytest.fixture
def fake_units(monkeypatch):
    units = []

    class SyntheticUnit(DirectUnit):
        stop_confirmed = True
        ready_error = False

        def launch(self, argv, *, environment_file=None):
            self._remaining()
            assert not self.launch_attempted
            self.launch_attempted = True
            self.process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            units.append(self)
            return self.process

        def wait_ready(self):
            if self.ready_error:
                raise ProtocolError("Synthetic group not verified")
            self.observed = True
            self.invocation = "a" * 32
            self.main_pid = self.process.pid
            self.group = self.cgroup_root / "system.slice" / self.name

        def ready(self):
            return self.observed

        def stop(self):
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=3)
            return self.stop_confirmed and self.observed

    monkeypatch.setattr("milo_home.work_process.DirectUnit", SyntheticUnit)
    yield SyntheticUnit, units
    for unit in units:
        unit.stop()
        for pipe in [unit.process.stdin, unit.process.stdout, unit.process.stderr]:
            pipe.close()


def states(case):
    with case[0][0].connect() as connection:
        return (
            connection.execute("SELECT state FROM execution_windows").fetchone()[0],
            connection.execute("SELECT status FROM work_tasks").fetchone()[0],
            connection.execute("SELECT count(*) FROM execution_start_intents").fetchone()[0],
            connection.execute("SELECT count(*) FROM execution_permits").fetchone()[0],
        )


def events(journal):
    with journal._connect() as connection:
        return [json.loads(row[0]) for row in connection.execute("SELECT payload FROM entries")]


def test_claim_resume_and_bounded_cleanup_never_launch_a_turn(launch_case, fake_units):
    case, _, journal, launcher, output, _ = launch_case
    task, window = reserve(case)
    with launcher.open(case[0][1], window["id"], credential="synthetic-secret") as conversation:
        assert conversation.state == "idle"
        assert states(case) == ("held", "queued", 1, 0)
        assert len(list(journal.path.parent.glob("*.env"))) == 1
        with pytest.raises(ApiError, match="worker_busy"):
            with execution_lock(case[0][0]):
                pytest.fail("The common host lock must cover the live process")
        with pytest.raises(ProtocolError, match="not authorized"):
            conversation.start("未接投递不许调用", input_id=task["initial_input"]["id"])
    assert states(case) == ("closed", "cancelled", 1, 0)
    assert not journal.outstanding()
    assert not list(journal.path.parent.glob("*.env"))
    process_events = [e for e in events(journal) if e["kind"] == "process"]
    assert [e["phase"] for e in process_events] == ["launch_intent", "ready", "stop_confirmed"]
    assert process_events[1]["main_pid"] == fake_units[1][0].process.pid
    assert "synthetic-secret" not in json.dumps(events(journal))
    requests = [json.loads(line) for line in output.read_text().splitlines()]
    assert [r["method"] for r in requests] == ["initialize", "initialized", "thread/resume"]
    assert requests[-1]["params"]["threadId"] == "synthetic-main"
    with pytest.raises(ApiError):
        with launcher.open(case[0][1], window["id"], credential="synthetic-secret"):
            pytest.fail("Cannot reuse a durable startup intent")
    assert len(fake_units[1]) == 1


@pytest.mark.parametrize("action", ["revoke", "pause", "stop", "deadline", "config"])
def test_change_during_preparation_blocks_spawn_and_cleans_own_credential(
    launch_case, fake_units, monkeypatch, action
):
    case, _, journal, launcher, _, _ = launch_case
    task, window = reserve(case)
    original = journal.record

    def change(stream, event):
        original(stream, event)
        if event.get("phase") != "launch_intent":
            return
        if action == "revoke":
            case[0][4].revoke(case[0][1])
        elif action == "pause":
            case[2].pause(True)
        elif action == "stop":
            case[0][5].stop(case[0][1], task["id"], "synthetic-stop")
        elif action == "deadline":
            case[-1][0] = window["deadline_at"]
        else:
            (journal.path.parent / "config.toml").write_bytes(b"changed")

    monkeypatch.setattr(journal, "record", change)
    with pytest.raises((ApiError, ProtocolError)):
        with launcher.open(case[0][1], window["id"], credential="synthetic-secret"):
            pytest.fail("Must not reach a live conversation")
    assert not fake_units[1]
    assert states(case)[0] == "closed"
    assert states(case)[1] in {"cancelled", "timed_out", "failed"}
    if action == "config":
        assert states(case)[1] == "failed"
    assert not journal.outstanding()
    assert not list(journal.path.parent.glob("*.env"))


@pytest.mark.parametrize("scenario", ["wrong-provider", "wrong-sandbox", "active"])
def test_effective_binding_failure_stops_process_but_keeps_business_unknown(
    launch_case, fake_units, scenario
):
    case, _, journal, launcher, _, selected = launch_case
    _, window = reserve(case)
    selected[0] = scenario
    with pytest.raises(ProtocolError):
        with launcher.open(case[0][1], window["id"], credential="synthetic-secret"):
            pytest.fail("Invalid resumed main must not be yielded")
    assert states(case) == ("unknown", "needs_reconciliation", 1, 0)
    assert fake_units[1][0].process.poll() is not None
    assert not list(journal.path.parent.glob("*.env"))


@pytest.mark.parametrize("failure", ["ready", "stop", "journal"])
def test_missing_process_or_durable_stop_evidence_preserves_occupancy(
    launch_case, fake_units, monkeypatch, failure
):
    case, _, journal, launcher, _, _ = launch_case
    _, window = reserve(case)
    if failure == "ready":
        fake_units[0].ready_error = True
    elif failure == "stop":
        fake_units[0].stop_confirmed = False
    else:
        original = journal.record

        def fail(stream, event):
            if event.get("phase") == "stop_confirmed":
                raise OSError("Synthetic durable stop failure")
            original(stream, event)

        monkeypatch.setattr(journal, "record", fail)
    with pytest.raises(ProtocolError):
        with launcher.open(case[0][1], window["id"], credential="synthetic-secret"):
            pass
    assert states(case) == ("unknown", "needs_reconciliation", 1, 0)
    assert fake_units[1][0].process.poll() is not None
    assert journal.outstanding()
    assert not list(journal.path.parent.glob("*.env"))


@pytest.mark.parametrize("failure", ["credential", "config", "version"])
def test_invalid_runtime_preflight_does_not_consume_startup_claim(
    launch_case, fake_units, monkeypatch, failure
):
    case, _, journal, launcher, _, _ = launch_case
    _, window = reserve(case)
    credential = "" if failure == "credential" else "synthetic-secret"
    if failure == "config":
        (journal.path.parent / "config.toml").write_bytes(b"changed")
    elif failure == "version":

        def reject(_, *, workspace):
            raise ValueError("Synthetic version mismatch")

        monkeypatch.setattr(CodexRuntime, "command", reject)
    with pytest.raises((ValueError, ProtocolError)):
        with launcher.open(case[0][1], window["id"], credential=credential):
            pytest.fail("Invalid preflight")
    assert states(case) == ("held", "queued", 0, 0)
    assert not fake_units[1] and not journal.outstanding()


@pytest.mark.skipif(not Path("/run/systemd/system").exists(), reason="Requires systemd")
def test_real_systemd_resume_and_cleanup_use_reserved_unit_and_generation(launch_case):
    case, _, journal, launcher, output, _ = launch_case
    _, window = reserve(case, seconds=20)
    with launcher.open(case[0][1], window["id"], credential="synthetic-secret"):
        row = next(e for e in events(journal) if e.get("phase") == "ready")
        pid = row["main_pid"]
        assert row["unit"] == window["unit_name"]
        fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
        generation = fields[19]
        assert len(row["invocation"]) == 32
    assert states(case) == ("closed", "cancelled", 1, 0)
    assert not journal.outstanding()
    assert not list(journal.path.parent.glob("*.env"))
    assert '"method": "turn/start"' not in output.read_text()
    path = Path("/proc") / str(pid) / "stat"
    if path.exists():
        fields = path.read_text().rsplit(") ", 1)[1].split()
        assert fields[19] != generation or fields[0] == "Z"


def test_lost_resume_response_is_not_replayed_after_process_stop(launch_case, fake_units):
    from milo_home.codex_rpc import DeliveryUnknown

    case, _, journal, launcher, output, selected = launch_case
    _, window = reserve(case)
    selected[0] = "resume-eof"
    with pytest.raises(DeliveryUnknown):
        with launcher.open(case[0][1], window["id"], credential="synthetic-secret"):
            pytest.fail("Cannot resume after a lost acknowledgement")
    assert states(case) == ("unknown", "needs_reconciliation", 1, 0)
    assert journal.outstanding()[0]["delivery"] == "unknown"
    assert output.read_text().count('"method": "thread/resume"') == 1
    assert fake_units[1][0].process.poll() is not None
    assert not list(journal.path.parent.glob("*.env"))


def test_existing_environment_file_is_never_overwritten_or_removed(
    launch_case, fake_units, monkeypatch
):
    case, runtime, journal, launcher, _, _ = launch_case
    _, window = reserve(case)
    original = runtime.claim_start
    existing = []

    def claim(*args, **kwargs):
        intent = original(*args, **kwargs)
        path = journal.path.parent / (intent.stream_id + ".env")
        path.write_bytes(b"preexisting synthetic evidence")
        path.chmod(0o600)
        existing.append(path)
        return intent

    monkeypatch.setattr(runtime, "claim_start", claim)
    with pytest.raises(FileExistsError):
        with launcher.open(case[0][1], window["id"], credential="synthetic-secret"):
            pytest.fail("Cannot replace prior private evidence")
    assert not fake_units[1]
    assert existing[0].read_bytes() == b"preexisting synthetic evidence"
    assert states(case)[0] == "closed"


def test_real_service_account_checkpoint_never_opens_root_files_as_service_uid(
    runtime_case, owned_database, fake_units, monkeypatch, tmp_path
):
    case, runtime, journal = runtime_case
    _, window = reserve(case)
    path, account = owned_database
    copied = path.parent / "seeded.sqlite3"
    case[0][0].backup(copied)
    os.chown(copied, account.pw_uid, account.pw_gid)
    os.replace(copied, path)
    runtime.database = ServiceDatabase(path, account.pw_name)
    with execution_lock(runtime.database) as lease:
        runtime.bind(case[0][1], case[0][-1]["id"], lease=lease)
    installed = CodexRuntime(Path("/synthetic/codex"), Path("/synthetic/host"))
    config = NewawzConfig(
        "synthetic-provider", "synthetic-model", "SYNTHETIC_KEY", SYNTHETIC_CONFIG
    )
    output = tmp_path / "service-requests.ndjson"
    peer = Path(__file__).parent / "fixtures/codex_app_server.py"
    monkeypatch.setattr(
        CodexRuntime,
        "command",
        lambda _, *, workspace: [sys.executable, str(peer), "normal", str(output)],
    )
    monkeypatch.setattr("milo_home.codex_process.time.time", lambda: case[-1][0])
    with WorkLauncher(runtime, config, installed).open(
        case[0][1], window["id"], credential="synthetic-secret"
    ):
        assert os.geteuid() == 0
    assert os.geteuid() == 0
    assert not journal.outstanding()
    assert all(entry.stat().st_uid == account.pw_uid for entry in path.parent.iterdir())
    assert all(entry.stat().st_uid == 0 for entry in journal.path.parent.iterdir())
    with runtime.database.connect() as connection:
        assert connection.execute("SELECT state FROM execution_windows").fetchone()[0] == "closed"


def test_partial_credential_write_is_removed_without_spawning(launch_case, fake_units, monkeypatch):
    case, _, journal, launcher, _, _ = launch_case
    _, window = reserve(case)

    def fail(_):
        raise OSError("Synthetic fsync failure")

    monkeypatch.setattr("milo_home.work_process.os.fsync", fail)
    with pytest.raises(OSError, match="fsync"):
        with launcher.open(case[0][1], window["id"], credential="synthetic-secret"):
            pytest.fail("Partial credentials cannot launch")
    assert not fake_units[1]
    assert not list(journal.path.parent.glob("*.env"))
    assert states(case) == ("closed", "failed", 1, 0)


def test_ambiguous_spawn_error_never_releases_or_retries(launch_case, fake_units, monkeypatch):
    case, _, journal, launcher, _, _ = launch_case
    _, window = reserve(case)
    calls = []

    def fail(self, *args, **kwargs):
        calls.append(self.name)
        self.launch_attempted = True
        raise OSError("Synthetic ambiguous spawn")

    monkeypatch.setattr(fake_units[0], "launch", fail)
    with pytest.raises(OSError, match="ambiguous"):
        with launcher.open(case[0][1], window["id"], credential="synthetic-secret"):
            pytest.fail("Cannot continue after ambiguous spawn")
    assert states(case) == ("unknown", "needs_reconciliation", 1, 0)
    assert journal.outstanding()
    assert not list(journal.path.parent.glob("*.env"))
    with pytest.raises(ApiError):
        with launcher.open(case[0][1], window["id"], credential="synthetic-secret"):
            pytest.fail("Cannot replay")
    assert calls == [window["unit_name"]]


def test_host_cpu_sample_blocks_spawn_without_relaxing_limit(launch_case, fake_units, monkeypatch):
    case, _, journal, launcher, _, _ = launch_case
    _, window = reserve(case)
    samples = iter([(100, 100), (200, 110)])
    monkeypatch.setattr(fake_units[0], "_cpu_sample", staticmethod(lambda: next(samples)))
    with pytest.raises(ProtocolError, match="CPU limit"):
        with launcher.open(case[0][1], window["id"], credential="synthetic-secret"):
            pytest.fail("Cannot spawn above host CPU threshold")
    assert not fake_units[1]
    assert states(case) == ("closed", "failed", 1, 0)
    assert not list(journal.path.parent.glob("*.env"))

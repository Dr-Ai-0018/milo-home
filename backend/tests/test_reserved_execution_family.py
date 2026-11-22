from types import SimpleNamespace

import pytest

from milo_home.codex_process import DirectUnit
from milo_home.codex_rpc import ProtocolError

UNIT = "milo-work-" + "a" * 32 + ".service"


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(wall=1000.0, monotonic=50.0)
    monkeypatch.setattr("milo_home.codex_process.time.time", lambda: value.wall)
    monkeypatch.setattr("milo_home.codex_process.time.monotonic", lambda: value.monotonic)
    monkeypatch.setattr(DirectUnit, "_cpu_sample", staticmethod(lambda: (100, 100)))
    return value


def reserved(**changes):
    values = dict(unit_name=UNIT, created_at=990, deadline_at=1020)
    values.update(changes)
    return DirectUnit.from_reservation(**values)


def capture_launch(monkeypatch):
    calls = []
    process = SimpleNamespace(poll=lambda: 0)

    def spawn(command, **kwargs):
        calls.append(command)
        return process

    monkeypatch.setattr("milo_home.codex_process.subprocess.Popen", spawn)
    return calls, process


def test_reserved_family_preserves_identity_and_consumes_preparation_time(clock, monkeypatch):
    calls, process = capture_launch(monkeypatch)
    unit = reserved()
    assert unit.name == UNIT
    assert unit.deadline == 70
    clock.wall += 5
    clock.monotonic += 5
    assert unit.launch(["/usr/bin/true"]) is process
    assert "--unit=" + UNIT in calls[0]
    assert "RuntimeMaxSec=15s" in calls[0]
    # Wrapper exit alone does not authorize another execution.
    with pytest.raises(ProtocolError, match="unavailable"):
        unit.launch(["/usr/bin/true"])
    assert len(calls) == 1


@pytest.mark.parametrize("wall,monotonic", [(1020, 51), (1001, 70)])
def test_either_deadline_stops_before_launch(clock, monkeypatch, wall, monotonic):
    calls, _ = capture_launch(monkeypatch)
    unit = reserved()
    clock.wall = wall
    clock.monotonic = monotonic
    with pytest.raises(ProtocolError, match="expired"):
        unit.launch(["/usr/bin/true"])
    assert calls == []
    with pytest.raises(ProtocolError, match="expired"):
        unit.checkpoint()


def test_wall_forward_jump_shortens_runtime(clock, monkeypatch):
    calls, _ = capture_launch(monkeypatch)
    unit = reserved()
    clock.wall = 1018
    clock.monotonic = 51
    unit.launch(["/usr/bin/true"])
    assert "RuntimeMaxSec=2s" in calls[0]


def test_clock_rollback_after_checkpoint_refuses_launch(clock, monkeypatch):
    calls, _ = capture_launch(monkeypatch)
    unit = reserved()
    clock.wall = 1005
    unit.checkpoint()
    clock.wall = 1004
    with pytest.raises(ProtocolError, match="backwards"):
        unit.launch(["/usr/bin/true"])
    assert calls == []


@pytest.mark.parametrize("created_at,deadline_at", [(1001, 1020), (990, 1000), (990, 999)])
def test_unusable_persisted_window_refused_at_construction(clock, created_at, deadline_at):
    with pytest.raises(ProtocolError):
        reserved(created_at=created_at, deadline_at=deadline_at)


@pytest.mark.parametrize(
    "changes",
    [
        {"unit_name": "other.service"},
        {"unit_name": "milo-work-" + "a" * 32 + ".service\n"},
        {"unit_name": None},
        {"created_at": True},
        {"deadline_at": 1020.0},
        {"deadline_at": 990},
        {"deadline_at": 4591},
    ],
)
def test_malformed_reservation_is_rejected(clock, changes):
    with pytest.raises(ValueError):
        reserved(**changes)


def test_spawn_error_is_not_retried(clock, monkeypatch):
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr("milo_home.codex_process.subprocess.Popen", fail)
    unit = reserved()
    with pytest.raises(OSError, match="synthetic"):
        unit.launch(["/usr/bin/true"])
    with pytest.raises(ProtocolError, match="unavailable"):
        unit.launch(["/usr/bin/true"])
    assert len(calls) == 1


def test_subsecond_remainder_cannot_be_rounded_up(clock, monkeypatch):
    calls, _ = capture_launch(monkeypatch)
    unit = reserved()
    clock.wall = 1019.5
    with pytest.raises(ProtocolError, match="expired"):
        unit.launch(["/usr/bin/true"])
    assert calls == []

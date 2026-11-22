import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from test_runs import pending

from milo_home.run_transport import (
    DISABLED_CODE_MODE_NOTICE,
    LEGACY_MULTI_AGENT_NOTICE,
    EventStream,
    stop_unit,
    unit_empty,
)


def test_disabled_capability_notice_does_not_hide_failures():
    def notice(message):
        return {
            "type": "item.completed",
            "item": {
                "type": "error",
                "message": message,
            },
        }

    benign = feed([notice(DISABLED_CODE_MODE_NOTICE), {"type": "turn.completed"}])
    assert benign.completed == 1 and not benign.failed and not benign.invalid
    legacy = feed([notice(LEGACY_MULTI_AGENT_NOTICE), {"type": "turn.completed"}])
    assert legacy.completed == 1 and not legacy.failed and not legacy.invalid
    altered = feed([notice(LEGACY_MULTI_AGENT_NOTICE + " unexpected"), {"type": "turn.completed"}])
    assert altered.failed
    unexpected = feed([notice("Unexpected failure"), {"type": "turn.completed"}])
    assert unexpected.failed
    terminal = feed([notice(DISABLED_CODE_MODE_NOTICE), {"type": "turn.failed"}])
    assert terminal.failed
    terminal_legacy = feed([notice(LEGACY_MULTI_AGENT_NOTICE), {"type": "turn.failed"}])
    assert terminal_legacy.failed
    top_level = feed([{"type": "error", "message": LEGACY_MULTI_AGENT_NOTICE}])
    assert top_level.failed


def feed(events):
    stream = EventStream()
    data = b"".join(json.dumps(event).encode() + b"\n" for event in events)
    for index in range(0, len(data), 7):
        stream.feed(data[index : index + 7], stdout=True)
    return stream


def test_event_completion_is_not_exit_code():
    good = feed(
        [
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message"},
            },
            {"type": "turn.completed"},
        ]
    )
    assert good.completed == 1 and not good.failed and not good.invalid
    bad = feed([{"type": "turn.failed"}, {"type": "turn.completed"}])
    assert bad.failed
    tool = feed(
        [
            {
                "type": "item.started",
                "item": {"type": "command_execution"},
            }
        ]
    )
    assert tool.invalid


def test_event_limits_and_invalid_input():
    for data in [b"not-json\n", b"[]\n", b'{"item":{}}\n']:
        stream = EventStream()
        stream.feed(data, stdout=True)
        assert stream.invalid
    stream = EventStream()
    stream.feed(b"x" * (2 * 1024 * 1024 + 1), stdout=False)
    assert stream.invalid


def test_bus_timeout_never_confirms_termination(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("systemctl", 5)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert unit_empty("milo-test") is False
    assert stop_unit("milo-test") is False


def test_transport_renews_pulse_with_its_start_identity(client, monkeypatch, tmp_path):
    from milo_home import run_transport as module

    thread, store = pending(client, 1)
    run = store.queue(thread, 60)
    quota = tmp_path / "cgroup/test"
    quota.mkdir(parents=True)
    (quota / "cpu.max").write_text("50000 100000")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("synthetic")
    tick = [0]
    beats = []

    def monotonic():
        tick[0] += 16
        return tick[0]

    heartbeat = store.heartbeat

    def record(run_id, identity):
        beats.append((run_id, heartbeat(run_id, identity)))

    monkeypatch.setattr(store, "heartbeat", record)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(module, "cpu_sample", lambda: (100, 100))
    monkeypatch.setattr(module, "unit_empty", lambda unit: True)
    monkeypatch.setattr(
        module,
        "Path",
        lambda value: tmp_path / "cgroup" if value == "/sys/fs/cgroup" else Path(value),
    )
    monkeypatch.setattr(
        module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="/test\n")
    )
    code, _, ended = module.execute(
        [sys.executable, "-c", "import time; time.sleep(0.6)"], prompt, store, run
    )
    assert code == 0 and ended
    assert beats and all(run_id == run and accepted for run_id, accepted in beats)

"""The timer monitor defers on host load and stops only its own child family."""

import sys

from milo_home import maintenance_monitor as monitor


def test_host_busy_defers_without_starting_a_process(monkeypatch, tmp_path):
    samples = iter([(100, 100), (200, 120)])
    monkeypatch.setattr(monitor, "cpu_sample", lambda: next(samples))
    marker = tmp_path / "never-created"
    command = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
    assert monitor.guarded(command) == 0
    assert not marker.exists()


def test_load_increase_stops_the_owned_child(monkeypatch, tmp_path):
    samples = iter([(100, 100), (200, 200), (300, 200)])
    monkeypatch.setattr(monitor, "cpu_sample", lambda: next(samples))
    marker = tmp_path / "child-state"
    program = (
        "from pathlib import Path; import time; "
        f"p=Path({str(marker)!r}); p.write_text('started'); "
        "time.sleep(30); p.write_text('finished')"
    )
    assert monitor.guarded([sys.executable, "-c", program]) == 80
    assert marker.read_text() == "started"

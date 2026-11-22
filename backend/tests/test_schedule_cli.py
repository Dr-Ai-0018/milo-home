import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

EXAMPLE = Path(__file__).parents[2] / "examples/schedule.json"


def test_real_schedule_management_entry(tmp_path):
    def command(*arguments, success=True):
        process = subprocess.run(
            [sys.executable, "-m", "milo_home.cli", "schedule", *arguments],
            env={
                "PATH": "/usr/bin:/bin",
                "MILO_DATABASE": str(tmp_path / "cli.sqlite3"),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert (process.returncode == 0) == success, process.stderr
        return json.loads(process.stdout) if success else process.stderr

    assert command("show")["phase"] == "unconfigured"
    assert command("configure", "--file", str(EXAMPLE))["configured"]
    assert command("pause")["phase"] == "paused"
    assert command("show")["paused"]
    deadline = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert command("override", "--phase", "resting", "--until", deadline)["phase"] == "paused"
    assert command("resume")["source"] == "override"
    assert command("clear-override")["source"] in {"weekly", "relaxed"}
    error = command("override", "--phase", "working", "--until", "2026-10-14T15:00", success=False)
    assert "Configuration or file check failed." in error
    assert "2026-10-14T15:00" not in error
    assert command("show")["automatic_wakeup"] is False

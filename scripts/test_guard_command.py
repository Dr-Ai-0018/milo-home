"""Regression checks for exact-scope cleanup; no CPU stress or model calls."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ENTRY = Path(__file__).with_name("guard-command.py")
SPEC = importlib.util.spec_from_file_location("development_guard", ENTRY)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class GuardChecks(unittest.TestCase):
    def test_missing_process_group_still_stops_exact_scope(self):
        process = Mock(pid=123)
        evidence = Mock(stdout="LoadState=not-found\n")
        with (
            patch.object(guard.os, "killpg", side_effect=ProcessLookupError),
            patch.object(guard.subprocess, "run", return_value=evidence) as run,
        ):
            guard.stop(process, "milo-validation-synthetic.scope")
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["systemctl", "stop", "milo-validation-synthetic.scope"],
        )

    def test_stop_timeout_cannot_be_reported_as_stopped(self):
        with (
            patch.object(guard.os, "killpg"),
            patch.object(
                guard.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("systemctl", 8),
            ),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            guard.stop(Mock(pid=123), "milo-validation-synthetic.scope")

    def test_populated_cgroup_cannot_be_reported_as_stopped(self):
        evidence = Mock(
            stdout="LoadState=loaded\nActiveState=active\nControlGroup=/synthetic\n"
        )
        with (
            patch.object(guard.os, "killpg"),
            patch.object(guard.subprocess, "run", return_value=evidence),
            self.assertRaises(RuntimeError),
        ):
            guard.stop(Mock(pid=123), "milo-validation-synthetic.scope")

    @unittest.skipUnless(os.geteuid() == 0, "Requires root systemd")
    def test_real_command_exit_removes_detached_scope_descendant(self):
        directory = Path(tempfile.mkdtemp(prefix="milo-guard-regression-"))
        output = directory / "pids.json"
        code = (
            "import os,json,subprocess,sys; from pathlib import Path; "
            "child=subprocess.Popen(['/bin/sleep','30'],start_new_session=True); "
            "Path(sys.argv[1]).write_text(json.dumps([child.pid,sorted(os.sched_getaffinity(0))]))"
        )
        result = subprocess.run(
            [sys.executable, str(ENTRY), sys.executable, "-c", code, str(output)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pid, affinity = json.loads(output.read_text())
        self.assertLessEqual(len(affinity), max(1, len(os.sched_getaffinity(0)) // 2))
        stat = Path("/proc", str(pid), "stat")
        self.assertTrue(
            not stat.exists() or stat.read_text().rsplit(") ", 1)[1].startswith("Z ")
        )


if __name__ == "__main__":
    unittest.main()

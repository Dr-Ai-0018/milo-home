"""Keep a synthetic tool descendant alive until the actual execution cgroup is stopped."""

import os
import subprocess
import sys
from pathlib import Path

child = subprocess.Popen(["/bin/sleep", "60"])
Path(sys.argv[2] + ".child").write_text(str(child.pid))
peer = Path(__file__).with_name("codex_app_server.py")
os.execv(sys.executable, [sys.executable, str(peer), *sys.argv[1:]])

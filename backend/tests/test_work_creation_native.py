import os
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from test_execution_control import execution_case as execution_case
from test_work_creation import call
from test_work_initialization import init_case as init_case
from test_work_tasks import work_case as work_case

from milo_home.codex_config import CodexRuntime, NewawzConfig
from milo_home.codex_journal import PrivateJournal
from milo_home.work_creation_records import CreationRecords, process_states

VERIFY_INSTALLED = CodexRuntime.verify
CLI = Path("/usr/bin/codex")
TOOL_HOST = Path(
    "/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/"
    "vendor/x86_64-unknown-linux-musl/bin/codex-code-mode-host"
)
pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not Path("/run/systemd/system").exists() or not TOOL_HOST.is_file(),
    reason="Requires explicit verified local Codex and root systemd",
)


@pytest.mark.parametrize("nested", [False, True])
def test_real_codex_creation_cli_persists_across_two_processes_without_a_turn(
    init_case, monkeypatch, tmp_path, nested
):
    installed = CodexRuntime(CLI, TOOL_HOST)
    monkeypatch.setattr(CodexRuntime, "verify", VERIFY_INSTALLED)
    config = NewawzConfig._validated(
        {},
        "synthetic-provider",
        "synthetic-model",
        {
            "name": "Synthetic initialization only",
            "base_url": "https://synthetic.invalid",
            "env_key": "SYNTHETIC_KEY",
            "wire_api": "responses",
            "requires_openai_auth": False,
        },
    )
    monkeypatch.setattr(NewawzConfig, "load", lambda: config)
    monkeypatch.setenv("SYNTHETIC_KEY", "synthetic-never-call-provider")
    repository = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--quiet", "--template=", str(repository)], check=True, timeout=5
    )
    local_config = repository / ".codex" / "config.toml"
    local_config.parent.mkdir()
    local_config.write_text('model = "untrusted-project-model"\n', encoding="utf-8")
    workspace = repository / "child" if nested else repository
    workspace.mkdir(exist_ok=True)
    _, _, _, workspace_id, access, _, _ = init_case[0][0]
    access.register_workspace(workspace_id, "Synthetic workspace", workspace)
    initial = (*init_case[:3], installed, config)
    call((initial,), monkeypatch, timeout=25)
    directory = initial[1]
    journal = PrivateJournal(directory, initialize=False)
    assert str(UUID(journal.binding().thread_id)) == journal.binding().thread_id
    assert journal.binding().cwd == workspace
    assert journal.binding().model == config.model
    assert (directory / "config.toml").read_bytes() == config.content
    assert journal.business_binding().session_id == initial[0][0][-1]["id"]
    records = CreationRecords(directory)
    methods = [e["request"]["method"] for e in records.events if e["kind"] == "intent"]
    assert methods == [
        "initialize",
        "thread/start",
        "thread/inject_items",
        "initialize",
        "thread/resume",
    ]
    states = process_states(records)
    assert len(states) == 2 and set(states.values()) == {"stop_confirmed"}
    assert not list(directory.glob("*.env"))

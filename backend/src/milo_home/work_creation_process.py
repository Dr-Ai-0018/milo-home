"""One bounded creation or resume-only host process; never starts a model turn."""

import os
import time
from pathlib import Path
from uuid import uuid4

from .codex_app_server import MainConversation
from .codex_config import private_read
from .codex_process import DirectUnit
from .codex_rpc import ProtocolError, StdioRpc
from .work_creation_records import marker_parameters, start_parameters, verify_result
from .work_stop import boot_id, identity_shape


def run_protocol(claim, *, binding=None):
    choice = claim.choice
    unit = DirectUnit.from_reservation(
        unit_name="milo-work-" + uuid4().hex + ".service",
        created_at=int(claim.limit.last_wall),
        deadline_at=int(claim.limit.wall_deadline),
    )
    unit.deadline = min(unit.deadline, claim.limit.monotonic_deadline)
    environment_file = claim.directory / (unit.name + ".env")
    owned_environment = False
    rpc = None

    def record(event):
        claim.records.record({**event, "connection": unit.name})

    def checkpoint():
        claim.checkpoint()
        unit.checkpoint()

    try:
        command = claim.installed.command(workspace=Path(choice["cwd"]))
        environment = claim.installed.environment(claim.directory, claim.config, claim.credential)
        checkpoint()
        descriptor = os.open(
            environment_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        owned_environment = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(environment)
            stream.flush()
            os.fsync(stream.fileno())
        while time.monotonic() < unit.next_cpu:
            checkpoint()
            time.sleep(0.02)
        record({"kind": "process", "phase": "launch_intent"})
        if private_read(claim.directory / "config.toml") != claim.config.content:
            raise ProtocolError("Creation configuration changed before launch")
        checkpoint()
        process = unit.launch(command, environment_file=environment_file)
        unit.wait_ready()
        identity = {
            "unit": unit.name,
            "group": "/" + str(unit.group.relative_to(unit.cgroup_root)),
            "boot_id": boot_id(),
            "invocation": unit.invocation,
            "main_pid": unit.main_pid,
        }
        identity_shape(identity)
        record({"kind": "process", "phase": "ready", **identity})
        checkpoint()
        rpc = StdioRpc(
            process, deadline=unit.deadline, record=record, stop=unit.stop, checkpoint=checkpoint
        )
        if binding is None:
            binding = _create(rpc, choice)
        else:
            conversation = MainConversation(rpc, binding, authorize_turn=lambda _: False)
            conversation.resume()
        checkpoint()
        if not unit.ready():
            raise ProtocolError("Creation execution identity changed")
        return binding
    finally:
        try:
            confirmed = (
                rpc.close() if rpc is not None else (unit.stop() if unit.launch_attempted else True)
            )
            if rpc is None and unit.process is not None:
                for pipe in [unit.process.stdin, unit.process.stdout, unit.process.stderr]:
                    pipe.close()
            record({"kind": "process", "phase": "stop_confirmed" if confirmed else "stop_unknown"})
            if not confirmed:
                raise ProtocolError("Creation process stop needs reconciliation")
        finally:
            if owned_environment:
                environment_file.unlink(missing_ok=True)


def _create(rpc, choice):
    def notification(message):
        if message["method"] not in {
            "thread/started",
            "thread/status/changed",
            "warning",
            "account/rateLimits/updated",
            "remoteControl/status/changed",
        }:
            raise ProtocolError("Unexpected event during main creation")

    rpc.on_notification = notification
    rpc.request(
        "initialize",
        {
            "clientInfo": {"name": "milo_home", "version": "0.1.0"},
            "capabilities": {"experimentalApi": False},
        },
    )
    rpc.notify_initialized()
    result = rpc.request("thread/start", start_parameters(choice))
    binding = verify_result(result, choice)
    rpc.request("thread/inject_items", marker_parameters(binding.thread_id))
    return binding

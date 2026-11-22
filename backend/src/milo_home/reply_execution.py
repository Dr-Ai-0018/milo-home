"""Service orchestration and persistent faults around a single pure-reply batch."""

import sqlite3
import time

from .automatic_services import AutomaticServices, require_entry
from .codex_config import NewawzConfig
from .codex_rpc import ProtocolError
from .errors import ApiError
from .reply_worker import ReplyWorker
from .revisit_queue import enabled as revisits_enabled
from .revisit_queue import mark_uncertain
from .service_observation import timer_active
from .work_credentials import load_credential


def run_once(
    database,
    directory,
    owner,
    profile,
    credential_file,
    seconds,
    stop,
    *,
    clock=time.time,
    model=None,
    native_home=None,
):
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ValueError("Reply timeout must be 1–3600 seconds")
    services = AutomaticServices(database, clock)
    with database.snapshot() as connection:
        entry = require_entry(connection, "reply", owner)
        native_required = native_home is None and revisits_enabled(connection, owner, int(clock()))
    if native_required:
        services.fault("reply", owner, "configuration_error")
        raise ValueError("Revisit-enabled service requires the configured native conversation")
    services.observe("reply", owner, timer_active=timer_active(entry["timer_unit"]))
    if not entry["enabled"] or entry["fault_code"]:
        return {"outcome": "service_disabled_or_faulted"}
    try:
        config = NewawzConfig.load(profile)
        if model is not None and native_home is None:
            config = config.with_model(model)
        if credential_file is None:
            raise ValueError("An explicit credential file is required")
        credential = load_credential(config, credential_file)
    except (OSError, ValueError):
        services.fault("reply", owner, "configuration_error")
        raise ValueError("Independent reply configuration or credential is invalid") from None
    next_observation = [time.monotonic() + 15]

    def progress():
        if time.monotonic() >= next_observation[0]:
            services.observe("reply", owner, timer_active=timer_active(entry["timer_unit"]))
            next_observation[0] = time.monotonic() + 15

    worker = None
    try:
        worker_type = ReplyWorker
        selection = {}
        if native_home is not None:
            from .native_reply_worker import NativeReplyWorker

            worker_type = NativeReplyWorker
            selection = {"native_home": native_home, "model": model}
        worker = worker_type(
            database,
            profile,
            config,
            credential,
            owner,
            runtime=directory,
            clock=clock,
            stop_requested=stop.is_set,
            progress=progress,
            **selection,
        )
        return worker.take_next(seconds)
    except ApiError as error:
        with database.connect(write=True) as connection:
            mark_uncertain(connection, owner, int(clock()))
        code = (
            error.code
            if error.code in {"termination_unknown", "publication_pending"}
            else "needs_reconciliation"
        )
        services.fault("reply", owner, code)
        raise
    except (OSError, ValueError, sqlite3.Error, ProtocolError):
        with database.snapshot() as connection:
            sealed = connection.execute(
                "SELECT 1 FROM reply_execution_results JOIN runs ON runs.id="
                "reply_execution_results.run_id WHERE runs.status='running' LIMIT 1"
            ).fetchone()
        code = "configuration_error" if worker is None else "interrupted"
        services.fault("reply", owner, "publication_pending" if sealed else code)
        raise

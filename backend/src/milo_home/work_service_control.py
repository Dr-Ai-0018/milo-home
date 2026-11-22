"""Latch registered recurring-entry faults without changing legacy commands."""

import sqlite3
import time
from contextlib import contextmanager

from .automatic_services import AutomaticServices, entry_available, require_entry
from .codex_rpc import ProtocolError
from .errors import ApiError
from .service_observation import timer_active


def observe_work_entry(database, owner, session, clock=time.time):
    with database.snapshot() as connection:
        row = connection.execute("SELECT * FROM automatic_services WHERE kind='work'").fetchone()
        if row is None:
            return None
        require_entry(connection, "work", owner)
        if row["session_id"] != session:
            raise ValueError("Automatic work session differs from registration")
    store = AutomaticServices(database, clock)
    store.observe("work", owner, timer_active=timer_active(row["timer_unit"]))
    return entry_available(row, int(clock()), store.boot, caller=True)


@contextmanager
def registered_work_errors(database, owner, *, configuration=False, active=True):
    try:
        yield
    except (ApiError, ProtocolError, OSError, ValueError, sqlite3.Error):
        with database.snapshot() as connection:
            row = connection.execute(
                "SELECT * FROM automatic_services WHERE kind='work' AND owner_id=?", (owner,)
            ).fetchone()
        if active and row is not None:
            AutomaticServices(database).fault(
                "work", owner, "configuration_error" if configuration else "needs_reconciliation"
            )
        raise

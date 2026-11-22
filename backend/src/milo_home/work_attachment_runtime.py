"""Optional attachment preparation and collection around a single execution."""

import logging
import sqlite3
from pathlib import Path

from .errors import ApiError
from .work_attachment_batches import (
    record_attachment_failure,
    register_attachment_attempt,
    seal_attachments,
)
from .work_attachment_source import prepare_task_directories, task_directories

LOGGER = logging.getLogger(__name__)


def collect_attachments(live, policy, store):
    if store is None or not live.attachments_enrolled:
        return
    database, task_id = live.runtime.database, live.intent.task_id
    try:
        if live.directories is not None:
            seal_attachments(
                database,
                live.owner,
                task_id,
                store=store,
                lease=live.lease,
                clock=live.runtime.clock,
                policy=policy,
            )
            return
    except (ApiError, OSError, ValueError, sqlite3.Error):
        LOGGER.warning("Work attachment sealing failed")
    try:
        record_attachment_failure(
            database,
            live.owner,
            task_id,
            lease=live.lease,
            clock=live.runtime.clock,
        )
    except (ApiError, sqlite3.Error):
        LOGGER.warning("Work attachment failure could not be recorded")


def prepare_directories(live, store):
    if store is None:
        return
    task_id = live.intent.task_id
    workspace = Path(live.runtime.journal.binding().cwd)
    try:
        live.attachments_enrolled = register_attachment_attempt(
            live.runtime.database, live.owner, task_id, lease=live.lease, clock=live.runtime.clock
        )
        if not live.attachments_enrolled:
            return
        store.check_workspace(workspace)
        prepare_task_directories(workspace, task_id)
    except (ApiError, OSError, ValueError, sqlite3.Error):
        LOGGER.warning("Work attachment directory preparation failed")
        return
    live.directories = task_directories(task_id)

"""Explicit service controls and fresh observations, never execution authority."""

import os
import re
import time

from .errors import ApiError
from .execution_control import execution_busy
from .execution_lock import execution_lock
from .management_lease import current_boot
from .reply_scope import ReplyScope
from .work_access import require_access, require_session

SERVICE_TTL = 75
FAULTS = {
    "configuration_error",
    "execution_failed",
    "invalid_result",
    "termination_unknown",
    "publication_pending",
    "interrupted",
    "needs_reconciliation",
}


def require_entry(connection, kind, owner):
    row = connection.execute("SELECT * FROM automatic_services WHERE kind=?", (kind,)).fetchone()
    if row is None or row["owner_id"] != owner:
        raise ApiError(409, "service_not_registered", "尚未登记此账号的独立服务。")
    if not service_database_matches(connection, row):
        raise ApiError(409, "service_database_changed", "服务登记属于原数据库，请先核对恢复状态。")
    return row


def service_database_matches(connection, row):
    if row is None:
        return False
    try:
        filename = next(
            item[2] for item in connection.execute("PRAGMA database_list") if item[1] == "main"
        )
        metadata = os.stat(filename)
        return (metadata.st_dev, metadata.st_ino) == (row["database_device"], row["database_inode"])
    except (OSError, StopIteration):
        return False


def observation_current(row, now, boot):
    return bool(
        row is not None
        and row["checked_boot"] == boot
        and row["checked_at"] is not None
        and row["updated_at"] <= row["checked_at"] <= now < row["checked_at"] + SERVICE_TTL
    )


def entry_available(row, now, boot, *, caller=False):
    return bool(
        row is not None
        and row["enabled"]
        and not row["fault_code"]
        and row["updated_at"] <= now
        and (caller or (observation_current(row, now, boot) and row["timer_active"]))
    )


class AutomaticServices:
    def __init__(self, database, clock=time.time, *, boot=None):
        self.database = database
        self.clock = clock
        self.boot = current_boot() if boot is None else boot

    def configure(
        self,
        kind,
        owner,
        timer_unit,
        *,
        session=None,
        enabled=False,
        revisits=None,
        work_decisions=None,
    ):
        if revisits is not None and (kind != "reply" or type(revisits) is not bool):
            raise ValueError("Only the native reply entry can explicitly enable revisits")
        if work_decisions is not None and (kind != "work" or type(work_decisions) is not bool):
            raise ValueError("Only the native work entry can enable work decisions")
        if kind not in {"reply", "work"} or not re.fullmatch(
            r"[A-Za-z0-9_.@-]+\.timer", timer_unit
        ):
            raise ValueError("An explicit service kind and timer unit are required")
        if (kind == "reply") != (session is None):
            raise ValueError("Only the work entry requires a session")
        with execution_lock(self.database), self.database.connect(write=True) as connection:
            if execution_busy(connection):
                raise ApiError(409, "run_busy", "已有执行占用，暂不能更换服务登记。")
            ReplyScope(owner).thread_id(connection)
            if connection.execute(
                "SELECT 1 FROM automatic_services WHERE kind!=? AND owner_id!=?", (kind, owner)
            ).fetchone():
                raise ValueError("Automatic entries must belong to the same explicit account")
            if session is not None:
                record = require_session(connection, owner, session)
                require_access(connection, owner, record["workspace_id"])
            previous = connection.execute(
                "SELECT * FROM automatic_services WHERE kind=?", (kind,)
            ).fetchone()
            restoring = previous is not None and not service_database_matches(connection, previous)
            if previous and previous["fault_code"] and not (restoring and not enabled):
                raise ApiError(409, "service_faulted", "先核对原故障，再显式恢复服务。")
            now = int(self.clock())
            if previous and now < previous["updated_at"]:
                raise ValueError("Service configuration clock moved backwards")
            connection.execute(
                "INSERT INTO automatic_services "
                "(kind,owner_id,session_id,timer_unit,database_device,database_inode,"
                "enabled,revision,updated_at) "
                "VALUES (?,?,?,?,?,?,?,1,?) ON CONFLICT(kind) DO UPDATE SET "
                "owner_id=excluded.owner_id,session_id=excluded.session_id,"
                "timer_unit=excluded.timer_unit,enabled=excluded.enabled,"
                "database_device=excluded.database_device,database_inode=excluded.database_inode,"
                "revision=automatic_services.revision+1,updated_at=excluded.updated_at,"
                "checked_at=NULL,checked_boot=NULL,timer_active=0",
                (
                    kind,
                    owner,
                    session,
                    timer_unit,
                    self.database.path.stat().st_dev,
                    self.database.path.stat().st_ino,
                    int(enabled),
                    now,
                ),
            )
            if revisits is not None:
                connection.execute(
                    "INSERT INTO reply_revisit_capabilities VALUES (?,?,?) "
                    "ON CONFLICT(owner_id) DO UPDATE SET enabled=excluded.enabled,"
                    "updated_at=excluded.updated_at",
                    (owner, int(revisits), now),
                )
            if work_decisions is not None:
                connection.execute(
                    "INSERT INTO work_decision_capabilities VALUES (?,?,?) "
                    "ON CONFLICT(owner_id) DO UPDATE SET enabled=excluded.enabled,"
                    "updated_at=excluded.updated_at",
                    (owner, int(work_decisions), now),
                )

    def observe(self, kind, owner, *, timer_active):
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            row = require_entry(connection, kind, owner)
            if now < max(row["updated_at"], row["checked_at"] or 0):
                raise ValueError("Service observation clock moved backwards")
            connection.execute(
                "UPDATE automatic_services SET checked_at=?,checked_boot=?,timer_active=? "
                "WHERE kind=?",
                (now, self.boot, int(timer_active), kind),
            )

    def fault(self, kind, owner, code):
        if code not in FAULTS:
            raise ValueError("Unknown service fault")
        with self.database.connect(write=True) as connection:
            require_entry(connection, kind, owner)
            connection.execute(
                "UPDATE automatic_services SET fault_code=coalesce(fault_code,?) WHERE kind=?",
                (code, kind),
            )

    def disable(self, kind, owner):
        # This closes only new claims, including when another entry holds the host lock.
        with self.database.connect(write=True) as connection:
            row = require_entry(connection, kind, owner)
            now = int(self.clock())
            if now < max(row["updated_at"], row["checked_at"] or 0):
                raise ValueError("Service control clock moved backwards")
            connection.execute(
                "UPDATE automatic_services SET enabled=0,revision=revision+1,updated_at=?,"
                "checked_at=?,checked_boot=? WHERE kind=?",
                (now, now, self.boot, kind),
            )

    def resume(self, kind, owner):
        with execution_lock(self.database), self.database.connect(write=True) as connection:
            row = require_entry(connection, kind, owner)
            if execution_busy(connection):
                raise ApiError(409, "run_busy", "原执行情况尚未核对，不能恢复新领取。")
            if connection.execute(
                "SELECT 1 FROM wake_dispatches WHERE settled_at IS NULL LIMIT 1"
            ).fetchone():
                raise ApiError(409, "dispatch_unsettled", "原回信派发尚未收口。")
            now = int(self.clock())
            if now < max(row["updated_at"], row["checked_at"] or 0):
                raise ValueError("Service control clock moved backwards")
            connection.execute(
                "UPDATE automatic_services SET enabled=1,fault_code=NULL,revision=revision+1,"
                "updated_at=?,checked_at=NULL,checked_boot=NULL,timer_active=0 WHERE kind=?",
                (now, kind),
            )

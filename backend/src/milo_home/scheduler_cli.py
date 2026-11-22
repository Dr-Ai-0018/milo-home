"""Explicit local scheduler management; installing a service is a separate action."""

import json
import os
import signal
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .automatic_budget import budget_view, load_policy
from .database import CURRENT_SCHEMA_VERSION
from .database_identity import ServiceDatabase
from .errors import ApiError
from .managed_stop import ManagedStop
from .management_lease import ManagementLease
from .scheduler import Scheduler
from .sqlite_readonly import private_database_file
from .worker import Worker


def add_commands(commands):
    scheduler = commands.add_parser("scheduler", help="显式检查或有限运行自动收发，不安装服务")
    actions = scheduler.add_subparsers(dest="scheduler_action", required=True)
    check = actions.add_parser("check", help="只读检查私有政策与现有运行额度")
    run = actions.add_parser("run", help="显式执行有限轮次，可能调用newawz并产生费用")
    for command in (check, run):
        command.add_argument("--policy", type=Path, required=True, help="私有政策JSON文件")
        command.add_argument("--timeout", type=int, default=3600)
    run.add_argument("--cycles", type=int, required=True)
    run.add_argument("--interval", type=int, default=15)
    run.add_argument("--execute", action="store_true", help="明确允许本次有限自动调用")


class StopRequest:
    """A main-thread signal flag; no locks are acquired by its setter."""

    def __init__(self):
        self.requested = False

    def set(self):
        self.requested = True

    def is_set(self) -> bool:
        return self.requested

    def wait(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self.requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.1))
        return self.requested


@contextmanager
def stop_signals(stop: StopRequest):
    previous = {}

    def request_stop(signum, frame):
        # Signal handlers must not access SQLite while effective UID is switched.
        stop.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def handle(database, args, runtime):
    executing = args.scheduler_action == "run"
    if executing and not args.execute:
        raise ValueError("Scheduler execution requires --execute")
    if not 1 <= args.timeout <= 3600:
        raise ValueError("Run timeout must be 1–3600 seconds")
    if executing and (args.cycles < 1 or not 1 <= args.interval <= 300):
        raise ValueError("Invalid scheduler cycle count or interval")
    policy = load_policy(args.policy)
    private_database_file(database.path)
    if database.path.stat().st_uid != os.geteuid() and not isinstance(database, ServiceDatabase):
        raise ValueError("Select the database owner before scheduler management")
    # No initialize(): missing or old databases must not be created/migrated by
    # check or by an execution request. Upgrade is an independent operation.
    with database.connect() as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if connection.execute("PRAGMA user_version").fetchone()[0] != CURRENT_SCHEMA_VERSION:
            raise ValueError("Scheduler requires the current schema")
        budget = budget_view(connection, policy, int(time.time()), args.timeout)
    if not executing:
        print(
            json.dumps(
                {
                    "automatic_wakeup": False,
                    "policy": policy.model_dump(),
                    "budget": budget,
                }
            )
        )
        return
    if budget["blocking_reason"]:
        raise ApiError(409, budget["blocking_reason"], "自动读信额度已用完或时钟异常。")
    stop = StopRequest()
    lease = ManagementLease(database, policy)
    managed = ManagedStop(stop, lease)
    worker = Worker(
        database,
        runtime=runtime,
        policy=policy,
        stop_requested=stop.is_set,
        progress=managed.progress,
    )
    coordinator = Scheduler(worker, interval=args.interval, timeout=args.timeout)
    with stop_signals(stop):
        lease.claim()
        try:
            report = coordinator.run(cycles=args.cycles, stop=managed)
        finally:
            lease.close()
    print(
        json.dumps(
            {
                **asdict(report),
                "automatic_execution_requested": True,
                "management_lease_lost": lease.lost,
            }
        )
    )
    if lease.lost or report.reason not in {"cycles_exhausted", "stop_requested", "run_cancelled"}:
        raise SystemExit(1)

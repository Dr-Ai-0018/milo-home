"""Explicit native policy registration; never resumes a thread or launches a model."""

import re
from uuid import UUID

from .codex_config import NewawzConfig
from .codex_journal import PrivateJournal
from .codex_rpc import ProtocolError
from .execution_control import execution_busy
from .execution_lock import execution_lock
from .native_policy import NativePolicyStore
from .work_runtime import WorkRuntime


def add_command(commands):
    command = commands.add_parser(
        "native-policy", help="查看或明确登记同一原生主对话的模型与自动审查，不执行"
    )
    command.add_argument("--owner", type=UUID, required=True)
    command.add_argument("--session", type=UUID, required=True)
    command.add_argument("--configure", action="store_true")
    command.add_argument("--model")
    command.add_argument("--expected", help="明确的当前digest；首次登记填none，防止覆盖并发更新")


def handle(database, directory, args):
    if args.configure and (
        not args.model
        or args.expected is None
        or (args.expected != "none" and not re.fullmatch(r"[0-9a-f]{64}", args.expected))
    ):
        raise ValueError("Configuration requires a model and explicit expected digest or none")
    if not args.configure and (args.model is not None or args.expected is not None):
        raise ValueError("Policy inspection does not accept configuration arguments")
    journal = PrivateJournal(directory, initialize=False, read_only=not args.configure)
    runtime = WorkRuntime(database, journal)
    owner, session = str(args.owner), str(args.session)
    with execution_lock(database) as lease:
        runtime.check_session_binding(owner, session, lease=lease)
        config = NewawzConfig.load()
        installed = journal.installation()
        installed.verify()
        journal.bind(journal.binding(), config, installed)
        store = NativePolicyStore(journal)
        current = store.read()
        if args.configure:
            if journal.outstanding():
                raise ProtocolError("Previous private execution still needs reconciliation")
            with database.snapshot() as connection:
                if execution_busy(connection):
                    raise ProtocolError("Existing execution must be settled before policy changes")
            expected = None if args.expected == "none" else args.expected
            current = store.configure(
                owner=owner,
                session=session,
                model=args.model,
                expected=expected,
                now=int(runtime.clock()),
            )
            lease.verify()
        return (
            current.summary()
            if current
            else {
                "registered": False,
                "execution_available": False,
            }
        )

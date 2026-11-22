"""Instantaneous read-only readiness; never a transferable execution permission."""

from .codex_rpc import ProtocolError
from .execution_lock import execution_lock
from .work_receiver import receive_context


def check_receiver(runtime, config, installed, owner, session_id, seconds, credential, stop):
    if not runtime.journal.read_only:
        raise ValueError("Preflight requires a read-only journal")
    with execution_lock(runtime.database) as lease:
        original = runtime.check_session_binding(owner, session_id, lease=lease)
        installed.verify()
        journal = runtime.journal
        journal.bind(journal.binding(), config, installed)
        installed.environment(journal.path.parent, config, credential)
        if journal.outstanding():
            raise ProtocolError("Previous private execution still needs reconciliation")
        if runtime.check_session_binding(owner, session_id, lease=lease) != original:
            raise ProtocolError("Preflight association or authorization changed")
        with runtime.database.connect() as connection:
            connection.execute("BEGIN")
            _, allowed = receive_context(
                connection, owner, session_id, original, int(runtime.clock()), seconds
            )
        runtime._identity(lease)
        if stop.is_set():
            raise ProtocolError("Preflight stopped")
    return {
        "ready": True,
        "execution_available": False,
        "requested_timeout": seconds,
        "allowed_timeout": allowed,
    }

"""Register an explicitly selected existing main; never start or invent a thread."""

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from .codex_app_server import ThreadBinding
from .codex_config import NewawzConfig, private_read, same_installation_identity
from .codex_journal import PrivateJournal
from .codex_rpc import ProtocolError
from .database_identity import private_run_directory
from .execution_control import execution_busy
from .execution_lock import execution_lock
from .work_access import require_access, require_session
from .work_creation_records import guard_registration
from .work_private_files import publish_file as _publish_file
from .work_private_files import sync_directory as _sync_directory
from .work_runtime import WorkRuntime

INTENT = "main-registration.json"


def add_init_command(commands):
    command = commands.add_parser(
        "init-main", help="初始化私密资料并登记该专用home内已有主thread，不创建或执行"
    )
    command.add_argument("--owner", type=UUID, required=True)
    command.add_argument("--session", type=UUID, required=True)
    command.add_argument("--thread", type=UUID, required=True, help="该专用home内已有主thread UUID")
    command.add_argument("--cli", type=Path, help="原Codex CLI绝对路径，也可由部署配置给出")
    command.add_argument("--tool-host", type=Path, help="原工具宿主绝对路径，也可由部署配置给出")


def _existing_journal(directory):
    path = directory / "session-journal.sqlite3"
    if path.exists() or path.is_symlink():
        journal = PrivateJournal(directory, initialize=False)
        if journal.outstanding():
            raise ProtocolError("Previous private execution still needs reconciliation")
        return journal
    if any(
        Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink()
        for suffix in ("-journal", "-wal", "-shm")
    ):
        raise ProtocolError("Private journal is missing while sidecars remain")
    return None


def _check_existing_main(journal, binding, installed, config, context, associated):
    if journal is None:
        if associated:
            raise ProtocolError("Associated private journal is missing; cannot initialize again")
        return
    stored = journal.binding_record()
    if stored is None:
        if associated:
            raise ProtocolError("Associated private main is missing")
        return
    if associated and journal.fingerprint() != associated["fingerprint"]:
        raise ProtocolError("Existing business main differs from the selected registration")
    try:
        if (
            journal.binding() != binding
            or journal.installation() != installed
            or stored["config_digest"] != config.digest
        ):
            raise ProtocolError("Existing private main differs from the selected registration")
        linked = journal.business_binding()
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ProtocolError("Existing private main is invalid") from None
    if linked is not None and (
        not associated
        or linked.binding_id != associated["binding_id"]
        or linked.fingerprint != journal.fingerprint()
        or any(
            getattr(linked, key) != context[key]
            for key in (
                "database",
                "database_device",
                "database_inode",
                "owner_id",
                "session_id",
                "workspace_id",
            )
        )
    ):
        raise ProtocolError("Existing private main belongs to another business association")


def _publish_journal(directory):
    # Initialize and commit the whole schema before making the final name
    # visible. A crash never leaves an empty/unversioned final journal.
    staging = directory / (".main-journal-" + uuid4().hex)
    staging.mkdir(mode=0o700)
    path = staging / "session-journal.sqlite3"
    try:
        PrivateJournal(staging)
        os.link(path, directory / path.name, follow_symlinks=False)
        _sync_directory(directory)
    finally:
        # Only these exact files were created by this invocation. Unexpected
        # sidecars keep the staging directory for inspection; never recurse.
        path.unlink(missing_ok=True)
        if not any(staging.iterdir()):
            staging.rmdir()
    return PrivateJournal(directory, initialize=False)


def _context(database, directory, owner, session_id, lease):
    path, device, inode = WorkRuntime(database, None)._identity(lease)
    with database.connect() as connection:
        connection.execute("BEGIN")
        session = require_session(connection, owner, session_id)
        access = tuple(require_access(connection, owner, session["workspace_id"]))
        root = Path(
            connection.execute(
                "SELECT root_path FROM workspaces WHERE id=?", (session["workspace_id"],)
            ).fetchone()[0]
        )
        if execution_busy(connection):
            raise ProtocolError("Existing execution occupancy needs reconciliation")
        associated = connection.execute(
            "SELECT binding_id,fingerprint FROM work_runtime_bindings WHERE session_id=?",
            (session_id,),
        ).fetchone()
    WorkRuntime(database, None)._identity(lease)
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise ProtocolError("Registered workspace is no longer canonical")
    return (
        {
            "database": path,
            "database_device": device,
            "database_inode": inode,
            "database_owner": lease.owner,
            "owner_id": owner,
            "session_id": session_id,
            "workspace_id": session["workspace_id"],
            "cwd": str(root),
            "codex_home": str(directory),
        },
        access,
        associated,
    )


def initialize_main(database, directory, owner, session_id, thread_id, installed):
    private_run_directory(directory)
    with execution_lock(database) as lease:
        return register_main_locked(
            database, directory, owner, session_id, thread_id, installed, lease
        )


def _publish_registration(path, intent):
    try:
        original_bytes = private_read(path)
    except FileNotFoundError:
        original_bytes = (json.dumps(intent, sort_keys=True) + "\n").encode()
    else:
        if not same_installation_identity(json.loads(original_bytes), intent):
            raise ProtocolError("Original main registration or configuration differs")
    _publish_file(path, original_bytes)


def register_main_locked(
    database, directory, owner, session_id, thread_id, installed, lease, *, expected_access=None
):
    context, access, associated = _context(database, directory, owner, session_id, lease)
    if expected_access is not None and access != expected_access:
        raise ProtocolError("Registration authorization changed")
    config = NewawzConfig.load()
    installed.verify()
    binding = ThreadBinding(thread_id, config.provider, config.model, Path(context["cwd"]))
    guard_registration(database, directory, binding, owner, session_id, lease, installed)
    journal = _existing_journal(directory)
    _check_existing_main(journal, binding, installed, config, context, associated)
    if (directory / "config.toml").exists() or (directory / "config.toml").is_symlink():
        if private_read(directory / "config.toml") != config.content:
            raise ProtocolError("Stored runtime configuration changed")
    intent = {
        "schema": 1,
        **context,
        "thread_id": binding.thread_id,
        "provider": config.provider,
        "model": config.model,
        "config_digest": config.digest,
        "cli": str(installed.cli),
        "tool_host": str(installed.tool_host),
    }
    current, current_access, _ = _context(database, directory, owner, session_id, lease)
    if current != context or current_access != access:
        raise ProtocolError("Registration identity or authorization changed")
    _publish_registration(directory / INTENT, intent)
    _publish_file(directory / "config.toml", config.content)
    if journal is None:
        journal = _publish_journal(directory)
    journal.bind(binding, config, installed)
    if journal.outstanding():
        raise ProtocolError("Previous private execution still needs reconciliation")
    result = WorkRuntime(database, journal).bind(
        owner, session_id, lease=lease, expected_access=access
    )
    return {
        "session_id": result.session_id,
        "private_main_registered": True,
        "association_recorded": True,
        "protocol_verified": False,
        "execution_available": False,
    }

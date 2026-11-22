"""Corroborate one provider failure with its original stopped native history."""

import json
import re

from .codex_rpc import ProtocolError
from .workspace_archive_files import read_file


def read_native_failure(home, name, thread_id):
    if not re.fullmatch(
        r"(?:sessions/\d{4}/\d{2}/\d{2}|archived_sessions)/rollout-[^/]+\.jsonl", name
    ):
        raise ProtocolError("Expected an original native rollout path")
    path = home / name
    if path.resolve() != path or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
        raise ProtocolError("Unexpected original native rollout identity")
    content, evidence = read_file(home, name, 8 * 1024 * 1024)
    lines = [json.loads(line) for line in content.splitlines()]
    if (
        not lines
        or lines[0].get("type") != "session_meta"
        or lines[0].get("payload", {}).get("id") != thread_id
    ):
        raise ProtocolError("Native rollout does not match the bound thread")
    return lines, evidence["sha256"]


def matching_failure(lines, thread_id, turn_id, entries, intent_sequence):
    # Never treat an error notification by itself as a terminal receipt.
    terminal = [
        line["payload"]
        for line in lines
        if line.get("type") == "event_msg"
        and isinstance(line.get("payload"), dict)
        and line["payload"].get("type") == "task_complete"
        and line["payload"].get("turn_id") == turn_id
    ]
    if len(terminal) != 1 or terminal[0].get("last_agent_message") is not None:
        return None
    native = terminal[0].get("error")
    if not isinstance(native, dict):
        return None
    status = _http_status(
        native.get("codex_error_info"), "http_connection_failed", "http_status_code"
    )
    if status is None:
        return None
    matches = []
    for sequence, entry in entries:
        message = entry.get("message", {})
        params = message.get("params", {})
        if (
            sequence > intent_sequence
            and entry.get("kind") == "notification"
            and message.get("method") == "error"
            and params.get("threadId") == thread_id
            and params.get("turnId") == turn_id
            and params.get("willRetry") is False
            and _http_status(
                params.get("error", {}).get("codexErrorInfo"),
                "httpConnectionFailed",
                "httpStatusCode",
            )
            == status
        ):
            matches.append(sequence)
    return matches[0] if len(matches) == 1 else None


def _http_status(info, kind, field):
    detail = info.get(kind) if isinstance(info, dict) else None
    value = detail.get(field) if isinstance(detail, dict) else None
    return value if type(value) is int and 500 <= value <= 599 else None

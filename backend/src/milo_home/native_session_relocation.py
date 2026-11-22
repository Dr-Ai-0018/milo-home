"""Relocate a compatible native index only in a new rehearsal copy."""

import json
import re
import sqlite3
from pathlib import Path
from uuid import UUID

from .codex_config import NewawzConfig
from .workspace_archive_files import DEFAULT_BUDGET, read_file


def rollout_facts(home, name):
    content, _ = read_file(home, name, DEFAULT_BUDGET)
    lines = [json.loads(line) for line in content.splitlines() if line]
    if not lines or not isinstance(lines[0], dict):
        raise ValueError("Native rollout metadata is missing")
    meta = lines[0]
    if meta.get("type") != "session_meta" or not isinstance(meta.get("payload"), dict):
        raise ValueError("Native rollout metadata is missing")
    turns = []
    started = set()
    for line in lines:
        if not isinstance(line, dict):
            raise ValueError("Native rollout has an unsupported record")
        payload = line.get("payload", {})
        if not isinstance(payload, dict):
            continue
        is_start = line.get("type") == "event_msg" and payload.get("type") == "task_started"
        if line.get("type") == "turn_context" or is_start:
            turn = payload.get("turn_id")
            if isinstance(turn, str) and turn not in turns:
                turns.append(turn)
            if is_start and isinstance(turn, str):
                started.add(turn)
    # Native compaction can retain a synthetic context without a model turn.
    # Keep any actual started turn even if its ID resembles that context marker.
    turns = [
        turn for turn in turns if turn in started or re.fullmatch(r"auto-compact-\d+", turn) is None
    ]
    return meta["payload"].get("id"), turns


def _native_index(home):
    indexes = sorted(home.glob("state_*.sqlite"))
    if len(indexes) != 1 or not re.fullmatch(r"state_\d+\.sqlite", indexes[0].name):
        raise ValueError("Native readback requires one unambiguous state index")
    return indexes[0]


def _index_rows(connection):
    tables = {row[1] for row in connection.execute("PRAGMA main.table_list") if row[2] == "table"}
    if not {"threads", "rollout_migration_skipped_rollouts"} <= tables:
        raise ValueError("Native state index lacks required rollout tables")
    try:
        skipped = connection.execute("SELECT count(*) FROM rollout_migration_skipped_rollouts")
        if skipped.fetchone()[0]:
            raise ValueError("Skipped rollout migrations require separate recovery")
        return connection.execute(
            "SELECT id, rollout_path, model_provider, model FROM threads"
        ).fetchall()
    except sqlite3.OperationalError:
        raise ValueError("Native state index lacks required rollout fields") from None


def relocate_rehearsal(home, manifest):
    source = Path(manifest["source"])
    mappings = []
    seen = set()
    chosen = None
    with sqlite3.connect(_native_index(home)) as connection:
        connection.execute("BEGIN")
        for thread, old_path, provider, model in _index_rows(connection):
            if not isinstance(thread, str) or str(UUID(thread)) != thread or thread in seen:
                raise ValueError("Native index requires unique canonical thread IDs")
            seen.add(thread)
            if not isinstance(old_path, str):
                raise ValueError("Native index has no canonical rollout path")
            old = Path(old_path)
            if not old.is_absolute() or old.as_posix() != old_path or ".." in old.parts:
                raise ValueError("Native index has no canonical rollout path")
            try:
                name = old.relative_to(source).as_posix()
            except ValueError:
                raise ValueError("Native index refers outside the captured home") from None
            if name not in manifest["artifacts"] or name.split("/")[0] not in {
                "sessions",
                "archived_sessions",
            }:
                raise ValueError("Native index refers to an uncaptured rollout")
            actual_id, turns = rollout_facts(home, name)
            if actual_id != thread:
                raise ValueError("Native index and rollout thread identities differ")
            mappings.append((str(home / name), thread))
            if thread == manifest["thread_id"]:
                chosen = {
                    "thread_id": thread,
                    "path": str(home / name),
                    "turn_ids": turns,
                    "provider": provider,
                    "model": model,
                }
        if chosen is None or not chosen["turn_ids"]:
            raise ValueError("Selected native thread has no verifiable historical turns")
        # Synthetic transport preserves model/provider identity, never production routing/auth.
        config = NewawzConfig._validated(
            {},
            chosen["provider"],
            chosen["model"],
            {
                "name": "Offline native restore verification",
                "base_url": "https://synthetic.invalid",
                "env_key": "MILO_NATIVE_READBACK_KEY",
                "wire_api": "responses",
                "requires_openai_auth": False,
            },
        )
        updated = connection.executemany("UPDATE threads SET rollout_path=? WHERE id=?", mappings)
        if updated.rowcount != len(mappings):
            raise ValueError("Native index did not relocate exactly the checked rows")
    config.materialize(home)
    return chosen, config, len(mappings)

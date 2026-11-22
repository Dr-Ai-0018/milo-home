"""Explicit local publication; parser construction never opens files or databases."""

import argparse
import json
import time
from pathlib import Path
from uuid import UUID

from .database import CURRENT_SCHEMA_VERSION


def positive_version(value):
    version = int(value)
    if version <= 0:
        raise argparse.ArgumentTypeError("条目版本必须是正整数")
    return version


def add_commands(commands):
    content = commands.add_parser("content", help="本机维护明确选择的公开内容，不读取私人材料")
    kinds = content.add_subparsers(dest="content_action", required=True)
    note = kinds.add_parser("note", help="近况草稿、修订、明确发布与撤下")
    actions = note.add_subparsers(dest="note_action", required=True)
    descriptions = {
        "create": "从UTF-8 JSON创建草稿，同ID重试不覆盖",
        "show": "核对最新草稿、当前公开内容和条目版本",
        "revise": "追加草稿修订，保留当前公开版本",
        "publish": "明确公开最新修订",
        "withdraw": "撤下公开内容，保留草稿与修订历史",
        "import-legacy": "明确导入三条已公开旧近况，不覆盖或复活已有条目",
    }
    for name, help_ in descriptions.items():
        action = actions.add_parser(name, help=help_)
        if name != "import-legacy":
            action.add_argument("--id", type=UUID, required=True)
        if name in {"create", "revise"}:
            action.add_argument("--file", type=Path, required=True)
        if name in {"revise", "publish", "withdraw"}:
            action.add_argument("--expected-version", type=positive_version, required=True)
        if name in {"publish", "import-legacy"}:
            action.add_argument("--confirm-public", action="store_true", required=True)
    return kinds


def handle(database, args, *, clock=time.time):
    from .public_notes import PublicNotes
    from .public_notes_models import PublicNoteInput

    with database.connect() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != CURRENT_SCHEMA_VERSION:
            raise ValueError("Public content requires the current schema; it does not migrate")
    service = PublicNotes(database, clock=clock)
    action = args.note_action
    if action in {"publish", "import-legacy"} and not args.confirm_public:
        raise ValueError("Publishing requires explicit confirmation")
    if action == "import-legacy":
        from .public_notes_legacy import import_legacy

        result = import_legacy(service)
    elif action == "show":
        result = service.show(str(args.id))
    elif action in {"create", "revise"}:
        content = PublicNoteInput.model_validate_json(args.file.read_text(encoding="utf-8"))
        if action == "create":
            result = service.create(str(args.id), content)
        else:
            result = service.revise(str(args.id), content, args.expected_version)
    elif action in {"publish", "withdraw"}:
        result = getattr(service, action)(str(args.id), args.expected_version)
    else:
        raise ValueError("Unknown public content operation")
    print(json.dumps(result, ensure_ascii=False))

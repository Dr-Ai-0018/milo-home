"""Local-only work publishing. Help never reads data or constructs asset storage."""

import json
from pathlib import Path
from uuid import UUID

from .database import CURRENT_SCHEMA_VERSION
from .public_asset_store import PublicAssetStore
from .public_notes_cli import positive_version
from .public_works import PublicWorks
from .public_works_models import PublicWorkInput


def add_commands(kinds):
    work = kinds.add_parser("work", help="作品完整草稿、明确发布与独立公开文件封存")
    actions = work.add_subparsers(dest="work_action", required=True)
    descriptions = {
        "create": "从UTF-8 JSON保存草稿，不自动公开",
        "show": "核对最新草稿、公开快照、条目版本及已封存文件",
        "revise": "按条目版本追加完整修订，不替换当前公开版",
        "publish": "核对全部选定文件后明确公开最新修订",
        "withdraw": "撤下当前公开快照与文件入口，不删除留存材料",
    }
    for name, description in descriptions.items():
        action = actions.add_parser(name, help=description)
        action.add_argument("--id", type=UUID, required=True)
        if name in {"create", "revise"}:
            action.add_argument("--file", type=Path, required=True)
        if name in {"revise", "publish", "withdraw"}:
            action.add_argument("--expected-version", type=positive_version, required=True)
        if name == "publish":
            action.add_argument("--confirm-public", action="store_true", required=True)
    asset = actions.add_parser("asset", help="显式封存作品文件，不发布或自动选入修订")
    operations = asset.add_subparsers(dest="asset_action", required=True)
    import_ = operations.add_parser("import", help="复制明确普通文件，同ID不替换固定字节")
    import_.add_argument("--id", type=UUID, required=True, help="所属作品ID")
    import_.add_argument("--asset-id", type=UUID, required=True, help="固定资产ID；换文件用新ID")
    import_.add_argument("--file", type=Path, required=True, help="明确选择的绝对源文件路径")
    import_.add_argument("--filename", help="可选下载显示名；只作元数据，不作寻址")


def handle(database, settings, args):
    with database.connect() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != CURRENT_SCHEMA_VERSION:
            raise ValueError("Public work commands require the current schema; they do not migrate")
    configuration = settings.public_assets
    store = (
        PublicAssetStore(
            configuration.root,
            max_bytes=configuration.max_bytes,
            owner=getattr(database, "uid", None),
        )
        if configuration
        else None
    )
    service = PublicWorks(database, store=store)
    action = args.work_action
    if action == "show":
        result = service.show(str(args.id))
    elif action in {"create", "revise"}:
        content = PublicWorkInput.model_validate_json(args.file.read_text(encoding="utf-8"))
        if action == "create":
            result = service.create(str(args.id), content)
        else:
            result = service.revise(str(args.id), content, args.expected_version)
    elif action == "asset":
        result = service.import_asset(
            str(args.id), str(args.asset_id), args.file, filename=args.filename
        )
    elif action in {"publish", "withdraw"}:
        if action == "publish" and not args.confirm_public:
            raise ValueError("Publishing requires explicit confirmation")
        result = getattr(service, action)(str(args.id), args.expected_version)
    else:
        raise ValueError("Unknown public work operation")
    print(json.dumps(result, ensure_ascii=False))

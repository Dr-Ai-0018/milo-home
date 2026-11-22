"""Local, explicit entry for a paired archive and offline recovery rehearsal."""

import json
from pathlib import Path

from .config import Settings
from .database import Database
from .database_identity import ServiceDatabase, private_run_directory
from .public_asset_store import PublicAssetStore
from .work_attachment_store import AttachmentStore
from .workspace_archive_files import DEFAULT_BUDGET


def add_commands(commands):
    command = commands.add_parser("backup-paired", help="采集或离线演练工作会话配套备份")
    actions = command.add_subparsers(dest="paired_action", required=True)
    capture = actions.add_parser("capture", help="在确认停写后生成一代私有备份")
    capture.add_argument("--workspace", type=Path, help="省略时使用部署配置工作区")
    capture.add_argument("--writers-stopped", action="store_true", required=True)
    capture.add_argument("--include", action="append", default=[])
    capture.add_argument("--exclude", action="append", default=[])
    capture.add_argument("--native-exclude", action="append", default=[])
    capture.add_argument(
        "--reply-directory",
        type=Path,
        help="明确选择原独立回信目录，将原生逐轮恢复路由纳入同代备份",
    )
    rehearse = actions.add_parser("rehearse", help="凭独立保存的摘要离线核对和恢复")
    rehearse.add_argument("--archive", type=Path, required=True)
    rehearse.add_argument("--sha256", required=True)
    for parser in (capture, rehearse):
        parser.add_argument("--out", type=Path, required=True)
        parser.add_argument("--budget-bytes", type=int, default=DEFAULT_BUDGET)


def handle(args):
    from .paired_backup import capture_generation, rehearse_generation

    if args.paired_action == "rehearse":
        if args.database_user or args.run_directory is not None:
            raise ValueError("Paired rehearsal does not use live database or runtime options")
        report = rehearse_generation(args.archive, args.out, args.sha256, budget=args.budget_bytes)
    else:
        if args.run_directory is None:
            raise ValueError("Paired capture requires an explicit private run directory")
        settings = Settings.from_env()
        database = (
            ServiceDatabase(settings.database, args.database_user)
            if args.database_user
            else Database(settings.database)
        )
        config = settings.work_attachments
        store = (
            AttachmentStore(
                config.root, max_bytes=config.max_bytes, owner=getattr(database, "uid", None)
            )
            if config
            else None
        )
        public = settings.public_assets
        public_store = (
            PublicAssetStore(
                public.root, max_bytes=public.max_bytes, owner=getattr(database, "uid", None)
            )
            if public
            else None
        )
        report = capture_generation(
            database,
            private_run_directory(args.run_directory),
            args.workspace,
            args.out,
            writers_stopped=args.writers_stopped,
            include=args.include,
            exclude=args.exclude,
            native_exclude=args.native_exclude,
            budget=args.budget_bytes,
            attachment_store=store,
            public_asset_store=public_store,
            reply_directory=args.reply_directory,
        )
    print(json.dumps(report, ensure_ascii=False))

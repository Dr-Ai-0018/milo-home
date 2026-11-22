"""Create a new, isolated synthetic database for frontend design work."""

import json
import os
import secrets
import tempfile
from pathlib import Path
from uuid import uuid4

from .auth import AuthService
from .config import Settings
from .conversations import ConversationService
from .database import Database
from .messages import MessageService
from .path_policy import PathPolicy
from .reply_result import ReplyResult
from .run_queue import queue_run
from .runs import RunStore
from .schedule_models import DayPlan, SchedulePolicy, Window
from .schedules import ScheduleStore

SCENARIOS = ("replied", "pending", "failed", "cancelled", "timed_out", "dual-entry")


def add_conversation_samples(database, owner: str, thread_id: str):
    conversations = ConversationService(database)
    letter, _ = conversations.create_letter(
        owner, thread_id, "【合成主题】周末的小作品", str(uuid4())
    )
    chat = conversations.chat(owner, thread_id)
    runs = RunStore(database)
    for conversation, bodies in [
        (letter, ["【合成样例】想做个可以慢慢添东西的小角落。"]),
        (chat, ["【合成样例】在家嘛？", "【合成样例】我带着新点子来啦！"]),
    ]:
        selected = []
        for body in bodies:
            message, _ = conversations.post(owner, conversation["id"], body, str(uuid4()))
            selected.append(message["id"])
        with database.connect(write=True) as connection:
            run_id = queue_run(connection, thread_id, 180, int(runs.clock()), selected)
        runs.started(run_id)
        replies = ReplyResult.model_validate(
            {
                "replies": [
                    {
                        "message_id": message_id,
                        "body": "【合成回信样例，非实际回信】\n\n"
                        "这段回信用于检查本会话的排版和回复关联。",
                    }
                    for message_id in selected
                ]
            }
        )
        if not runs.publish(run_id, replies):
            raise ValueError("Synthetic conversation reply could not be saved")
        conversations.post(
            owner, conversation["id"], "【合成样例】这条续轮还在等待回信。", str(uuid4())
        )
    waiting, _ = conversations.create_letter(
        owner, thread_id, "【合成主题】留给明天的信", str(uuid4())
    )
    conversations.post(owner, waiting["id"], "【合成样例】这个主题还没有收到回信。", str(uuid4()))


def create_fixture(parent: Path, scenario: str) -> Path:
    """Never open an existing database or consult MILO_DATABASE/provider settings."""
    if scenario not in SCENARIOS:
        raise ValueError("Unknown preview scenario")
    if not parent.is_absolute() or not parent.is_dir():
        raise ValueError("Preview parent must be an existing absolute directory")
    # Validate the resolved destination before creating anything.
    paths = PathPolicy.configured()
    paths.require_external(parent.resolve(), directory=True)
    directory = Path(tempfile.mkdtemp(prefix="milo-home-preview-", dir=parent.resolve()))
    settings = Settings(
        database=directory / "home.sqlite3",
        origins=("http://127.0.0.1:7473",),
        secure_cookie=False,
        paths=paths,
    )
    # Leave an incomplete marker on failure, rather than attempting recursive cleanup.
    marker = directory / "SYNTHETIC-PREVIEW.txt"
    marker.write_text("INCOMPLETE synthetic preview; no real messages or model calls.\n")
    database = Database(settings.database, paths=paths)
    database.initialize()
    password = secrets.token_urlsafe(24)
    auth = AuthService(database, settings)
    token = auth.create_invitation("preview")
    user, session = auth.activate(token, password, "来访者（合成样例）")
    auth.logout(session)
    messages = MessageService(database)
    thread_id = messages.list_threads(user["id"])[0]["id"]
    first, _ = messages.post(
        user["id"], thread_id, "【合成样例】早上好！来看看小家。", str(uuid4())
    )
    second, _ = messages.post(
        user["id"],
        thread_id,
        "【合成样例】这是一封用于检查长文本和换行的来信。\n\n"
        + "希望手机上也能舒服地读完，按钮和正文各有自己的位置。\n" * 12,
        str(uuid4()),
    )
    runs = RunStore(database)
    if scenario != "pending":
        run_id = runs.queue(thread_id, 180)
        runs.started(run_id)
        result = ReplyResult.model_validate(
            {
                "replies": [
                    {
                        "message_id": first["id"],
                        "body": "【合成回信样例，非实际回信】来啦，先坐一会儿。",
                    },
                    {
                        "message_id": second["id"],
                        "body": "【合成回信样例，非实际回信】\n\n"
                        + "这段文字只用于查看回信的排版、行距和阅读节奏。\n\n" * 10,
                    },
                ]
            }
        )
        if not runs.publish(run_id, result):
            raise ValueError("Synthetic reply could not be saved")
        messages.post(user["id"], thread_id, "【合成样例】这封新留言还没有回信。", str(uuid4()))
        if scenario not in {"replied", "dual-entry"}:
            run_id = runs.queue(thread_id, 180)
            runs.started(run_id)
            if scenario != "failed":
                runs.stop(run_id, scenario)
            runs.finish_failed(run_id, "execution_failed")
    if scenario == "dual-entry":
        add_conversation_samples(database, user["id"], thread_id)
    plan = DayPlan(
        windows=[
            Window(start="09:00", end="18:00", phase="working"),
            Window(start="18:00", end="19:00", phase="winding_down"),
        ],
        fallback=["09:00", "16:00"],
    )
    ScheduleStore(database).configure(SchedulePolicy(weekdays=plan, days_off=plan))
    descriptor = os.open(directory / "login.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(
            {
                "synthetic": True,
                "handle": "preview",
                "password": password,
                "thread_id": thread_id,
                "scenario": scenario,
            },
            stream,
            ensure_ascii=False,
            indent=2,
        )
        stream.write("\n")
    marker.write_text(
        "READY synthetic preview; all messages/replies are explicitly labelled samples.\n"
        "No model call or service was started. Never deploy this database.\n",
        encoding="utf-8",
    )
    return directory

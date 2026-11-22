import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from milo_home.database import Database
from milo_home.errors import ApiError
from milo_home.work_access import WorkAccess
from milo_home.work_tasks import WorkTasks


@pytest.fixture
def work_case(tmp_path):
    database = Database(tmp_path / "work.sqlite3")
    database.initialize()
    owner, other, workspace = (str(uuid4()) for _ in range(3))
    with database.connect(write=True) as connection:
        for index, user in enumerate([owner, other]):
            connection.execute(
                "INSERT INTO users VALUES (?,?,?,'unused',1)",
                (user, f"visitor{index}", "同一个称呼"),
            )
            connection.execute("INSERT INTO threads VALUES (?,?,1)", (str(uuid4()), user))
    access = WorkAccess(database, clock=lambda: 100)
    access.register_workspace(workspace, "合成工作区", tmp_path)
    access.grant(owner)
    tasks = WorkTasks(database, clock=lambda: 101)
    session, _ = tasks.main_session(owner, workspace)
    return database, owner, other, workspace, access, tasks, session


def create(case, text="合成任务", key=None):
    _, owner, _, _, _, tasks, session = case
    return tasks.create(owner, session["id"], text, key or str(uuid4()))[0]


def test_task_input_retries_are_durable_and_never_enter_legacy_messages(work_case, tmp_path):
    database, owner, _, _, _, tasks, session = work_case
    key = str(uuid4())
    task = create(work_case, key=key)
    reopened = WorkTasks(Database(database.path))
    retried, created = reopened.create(owner, session["id"], "合成任务", key)
    assert not created and retried == task
    assert task["status"] == "queued" and task["execution_available"] is False
    assert task["initial_input"]["delivery"] == "queued"
    assert reopened.get(owner, task["id"]) == task
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    backup = tmp_path / "backup.sqlite3"
    database.backup(backup)
    restored = Database(backup)
    restored.initialize()
    assert WorkTasks(restored).get(owner, task["id"]) == task


def test_concurrent_same_key_saves_one_task_and_one_input(work_case):
    _, owner, _, _, _, tasks, session = work_case
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(lambda _: tasks.create(owner, session["id"], "并发", key), range(4))
        )
    assert sum(created for _, created in results) == 1
    assert len({item["id"] for item, _ in results}) == 1


def test_idempotency_namespace_cannot_move_text_or_kind_between_tasks(work_case):
    _, owner, _, _, _, tasks, session = work_case
    key = str(uuid4())
    first = create(work_case, key=key)
    second = create(work_case)
    with pytest.raises(ApiError, match="idempotency_conflict"):
        tasks.create(owner, session["id"], "改正文", key)
    with pytest.raises(ApiError, match="idempotency_conflict"):
        tasks.append(owner, second["id"], "合成任务", key)
    extra_key = str(uuid4())
    extra, saved = tasks.append(owner, first["id"], "补充", extra_key)
    assert saved
    assert tasks.append(owner, first["id"], "补充", extra_key) == (extra, False)
    with pytest.raises(ApiError, match="idempotency_conflict"):
        tasks.append(owner, second["id"], "补充", extra_key)


def test_pagination_and_input_stop_retry_keep_identity(work_case):
    _, owner, _, _, _, tasks, session = work_case
    first = create(work_case)
    second = create(work_case)
    page = tasks.list(owner, session["id"], 0, 1)
    assert [row["id"] for row in page["tasks"]] == [first["id"]]
    assert tasks.list(owner, session["id"], page["next_after"], 1)["tasks"][0]["id"] == second["id"]
    extra, _ = tasks.append(owner, first["id"], "新输入", str(uuid4()))
    assert tasks.inputs(owner, first["id"], 1, 1)["inputs"] == [extra]
    stop_key = str(uuid4())
    stopped = tasks.stop(owner, first["id"], stop_key)
    assert stopped["status"] == "cancelled"
    assert tasks.stop(owner, first["id"], stop_key) == stopped
    assert all(
        row["delivery"] == "cancelled" for row in tasks.inputs(owner, first["id"], 0, 50)["inputs"]
    )
    with pytest.raises(ApiError, match="work_input_conflict"):
        tasks.append(owner, first["id"], "不能重启", str(uuid4()))
    with pytest.raises(ApiError, match="idempotency_conflict"):
        tasks.stop(owner, second["id"], stop_key)


def test_revocation_and_regrant_do_not_resurrect_old_tasks(work_case):
    database, owner, other, workspace, access, tasks, session = work_case
    task = create(work_case)
    access.revoke(owner)
    assert access.workspaces(owner) == [] and access.workspaces(other) == []
    for user in [owner, other]:
        with pytest.raises(ApiError, match="work_not_found"):
            tasks.get(user, task["id"])
    access.grant(owner)
    assert tasks.main_session(owner, workspace) == (session, False)
    assert tasks.list(owner, session["id"], 0, 50)["tasks"] == []
    with pytest.raises(ApiError, match="work_not_found"):
        tasks.create(owner, session["id"], "合成任务", _initial_key(database, task["id"]))
    assert create(work_case)["id"] != task["id"]
    with pytest.raises(ApiError, match="main_operator_bound"):
        access.grant(other)


def _initial_key(database, task_id):
    with database.connect() as connection:
        return connection.execute(
            "SELECT client_key FROM work_inputs WHERE task_id=?", (task_id,)
        ).fetchone()[0]


def test_workspace_change_hides_old_task_and_stops_active_without_finishing(work_case, tmp_path):
    database, owner, _, workspace, access, tasks, _ = work_case
    task = create(work_case)
    with database.connect(write=True) as connection:
        connection.execute("UPDATE work_tasks SET status='running' WHERE id=?", (task["id"],))
    new_root = tmp_path / "next"
    new_root.mkdir()
    access.register_workspace(workspace, "另一个位置", new_root)
    with pytest.raises(ApiError, match="work_not_found"):
        tasks.get(owner, task["id"])
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM work_tasks").fetchone()[0] == "stopping"


def test_task_insert_and_initial_input_are_one_transaction(work_case):
    database, *_ = work_case
    with database.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_input BEFORE INSERT ON work_inputs "
            "BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic"):
        create(work_case)
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM work_tasks").fetchone()[0] == 0


def test_queue_capacity_still_allows_idempotent_retry(work_case):
    _, owner, _, _, _, tasks, session = work_case
    key = str(uuid4())
    first = create(work_case, key=key)
    for _ in range(31):
        create(work_case)
    assert tasks.create(owner, session["id"], "合成任务", key)[0] == first
    with pytest.raises(ApiError, match="work_queue_full"):
        create(work_case)
    tasks.stop(owner, first["id"], str(uuid4()))
    assert create(work_case)["status"] == "queued"


def test_input_capacity_preserves_retry_and_sequence(work_case):
    _, owner, _, _, _, tasks, _ = work_case
    task = create(work_case)
    key = str(uuid4())
    extra, _ = tasks.append(owner, task["id"], "补充", key)
    for _ in range(98):
        tasks.append(owner, task["id"], "更多", str(uuid4()))
    assert tasks.append(owner, task["id"], "补充", key) == (extra, False)
    with pytest.raises(ApiError, match="work_inputs_full"):
        tasks.append(owner, task["id"], "超额", str(uuid4()))


def test_database_scope_and_immutable_input_constraints(work_case):
    database, owner, other, _, _, _, _ = work_case
    task = create(work_case)
    with database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE work_inputs SET body='替换原文' WHERE task_id=?", (task["id"],)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE work_sessions SET owner_id=?", (other,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO work_inputs VALUES (?,?,?,2,'supplement','越权',?,'queued',1)",
                (str(uuid4()), task["id"], other, str(uuid4())),
            )
        assert connection.execute("SELECT owner_id FROM work_tasks").fetchone()[0] == owner

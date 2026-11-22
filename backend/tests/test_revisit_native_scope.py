"""Read v1 ledgers and route revisits without claiming the original message a second time."""

from uuid import uuid4

import pytest
from test_native_messages import message_case as message_case
from test_native_policy import execution_case as execution_case
from test_native_policy import native_case as native_case
from test_native_policy import runtime_case as runtime_case
from test_native_policy import work_case as work_case

from milo_home.codex_rpc import ProtocolError
from milo_home.execution_lock import execution_lock
from milo_home.native_message_scope import NativeMessageScope, NativeRevisitContext


def test_old_scope_bytes_and_ledger_read_survive_revisit_upgrade(message_case):
    runtime, store, original = message_case
    with execution_lock(runtime.database) as lease:
        store.claim(original, lease=lease)
    with store._connect(write=True) as connection:
        birth = tuple(connection.execute("SELECT * FROM deliveries").fetchone())
        connection.execute("DROP TABLE revisits")
        connection.execute("PRAGMA user_version=1")
    assert store.inspect(original.run_id)[0] == original
    assert "revisit" not in original.model_dump(mode="json")
    assert original.encoded() == birth[1] and original.digest == birth[2]
    scope = NativeMessageScope.model_validate(
        original.model_dump()
        | {
            "run_id": uuid4(),
            "message_ids": (),
            "revisit": NativeRevisitContext(
                id=uuid4(), revision=1, due_at=100, source_ids=original.message_ids
            ),
        }
    )
    with execution_lock(runtime.database) as lease:
        store.claim(scope, lease=lease)
        duplicate = NativeMessageScope.model_validate(scope.model_dump() | {"run_id": uuid4()})
        with pytest.raises(ProtocolError, match="earlier delivery"):
            store.claim(duplicate, lease=lease)
    assert store.inspect(scope.run_id)[0] == scope
    assert store.inspect(original.run_id)[0] == original
    with store._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM deliveries WHERE run_id=?", (str(original.run_id),)
                ).fetchone()
            )
            == birth
        )
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == len(
            original.message_ids
        )

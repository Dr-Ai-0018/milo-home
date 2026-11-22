import json
import time
from uuid import uuid4

import pytest
from test_codex_app_server import peer as peer
from test_execution_budget import setup
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_tasks import work_case as work_case

from milo_home.codex_rpc import DeliveryUnknown, ProtocolError
from milo_home.errors import ApiError


@pytest.mark.parametrize("launch_limit", [1, 2])
def test_real_adapter_callback_consumes_fresh_permit_for_each_turn(
    execution_case, peer, launch_limit
):
    work, _, _, _ = execution_case
    budget, permits = setup(execution_case, launches=launch_limit)
    task, window = reserve(execution_case)
    extra, _ = work[5].append(work[1], task["id"], "第二轮增量", str(uuid4()))
    session, _, _, _, output = peer()
    issued = {}

    def authorize(input_id):
        try:
            permit = permits.reserve(work[1], window["id"], input_id)
        except ApiError:
            return False
        issued[input_id] = permit
        return permits.consume(work[1], permit["id"], input_id)

    session.authorize_turn = authorize
    session.resume()
    first = task["initial_input"]["id"]
    session.start("第一轮增量", input_id=first)
    assert session.wait() == "completed"
    assert permits.finish(issued[first]["id"], delivery_known=True)
    with pytest.raises(ProtocolError, match="not authorized"):
        session.start("重复输入不能重发", input_id=first)
    if launch_limit == 1:
        with pytest.raises(ProtocolError, match="not authorized"):
            session.start("没有额度的第二轮", input_id=extra["id"])
    else:
        session.start("第二轮增量", input_id=extra["id"])
        assert session.wait() == "completed"
        assert permits.finish(issued[extra["id"]]["id"], delivery_known=True)
    requests = [json.loads(line) for line in output.read_text().splitlines()]
    starts = [row for row in requests if row["method"] == "turn/start"]
    assert len(starts) == launch_limit
    assert budget.view()["held_or_charged_launches"] == launch_limit
    assert budget.view()["held_or_charged_seconds"] == 60


def test_lost_adapter_response_keeps_spent_permit_and_window(execution_case, peer):
    work, control, _, _ = execution_case
    budget, permits = setup(execution_case, launches=1)
    task, window = reserve(execution_case)
    input_id = task["initial_input"]["id"]
    permit = permits.reserve(work[1], window["id"], input_id)
    session, rpc, _, _, output = peer("lost")
    session.authorize_turn = lambda key: permits.consume(work[1], permit["id"], key)
    session.resume()
    rpc.deadline = time.monotonic() + 0.2
    with pytest.raises(DeliveryUnknown):
        session.start("可能执行过", input_id=input_id)
    assert not permits.finish(permit["id"], delivery_known=False)
    assert not control.close(window["id"], stop=lambda: True, delivery_known=True, outcome="failed")
    assert budget.view()["held_or_charged_launches"] == 1
    assert budget.view()["held_or_charged_seconds"] == 60
    assert output.read_text().count('"method": "turn/start"') == 1

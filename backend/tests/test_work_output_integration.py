import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from test_execution_control import execution_case as execution_case
from test_execution_control import reserve
from test_work_process import fake_units as fake_units
from test_work_process import launch_case as launch_case
from test_work_runtime import runtime_case as runtime_case
from test_work_tasks import work_case as work_case

from milo_home.work_models import WorkEventPage, WorkResultList
from milo_home.work_progress import WorkProgress
from milo_home.work_recovery import WorkRecovery
from milo_home.work_task_receipt import read_receipt

EXAMPLES = Path(__file__).resolve().parents[2] / "examples/work-progress.json"
pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Host lease requires root")


def normalize(value, name):
    """Anonymize generated identifiers/timestamps while retaining all relationships."""
    identities = {}

    def visit(value, key=None):
        if key in {"observed_at", "recorded_at"}:
            return 1792231200
        if isinstance(value, dict):
            return {k: visit(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v) for v in value]
        if isinstance(value, str):
            try:
                UUID(value)
            except ValueError:
                return value
            if value not in identities:
                identities[value] = str(uuid5(NAMESPACE_URL, f"example:{name}:{len(identities)}"))
            return identities[value]
        return value

    return visit(value)


@pytest.mark.parametrize(
    "name", ["completed", "completed-turn-stopped-task", "unknown-without-receipt"]
)
def test_real_trigger_timelines_match_examples(launch_case, fake_units, monkeypatch, name):
    case, runtime, journal, launcher, _, _ = launch_case
    task, window = reserve(case)
    if name == "completed-turn-stopped-task":
        original = fake_units[0].stop

        def stop(unit):
            confirmed = original(unit)
            case[0][5].stop(case[0][1], task["id"], str(uuid4()))
            return confirmed

        monkeypatch.setattr(fake_units[0], "stop", stop)
    if name == "unknown-without-receipt":
        original = journal.record

        def fail(stream, event):
            if event["kind"] == "work_terminal":
                raise OSError("synthetic missing receipt")
            return original(stream, event)

        monkeypatch.setattr(journal, "record", fail)
        with pytest.raises(OSError):
            launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    else:
        launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    progress = WorkProgress(case[0][0])
    results = progress.results(case[0][1], task["id"])
    events = progress.events(case[0][1], task["id"], limit=50)
    WorkResultList.model_validate(results)
    WorkEventPage.model_validate(events)
    assert [e["sequence"] for e in events["events"]] == list(range(1, events["next_after"] + 1))
    assert [(e["kind"], e["state"]) for e in events["events"][:2]] == [
        ("task_status", "queued"),
        ("input_delivery", "queued"),
    ]
    sample = normalize(
        {
            "name": name,
            "task_status": case[0][5].get(case[0][1], task["id"])["status"],
            "results": results,
            "events": events,
        },
        name,
    )
    destination = os.environ.get("MILO_SYNTHETIC_EXAMPLES")
    if destination:
        Path(destination, name + ".json").write_text(
            json.dumps(sample, ensure_ascii=False, indent=2) + "\n"
        )
    else:
        examples = json.loads(EXAMPLES.read_text())
        assert sample == next(s for s in examples["scenarios"] if s["name"] == name)


def test_sensitive_final_answer_is_sealed_withheld_and_replay_never_reextracts(
    launch_case, fake_units, monkeypatch
):
    case, runtime, journal, launcher, output, selected = launch_case
    selected[0] = "secret-answer"
    task, window = reserve(case)
    launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    progress = WorkProgress(case[0][0])
    result = progress.results(case[0][1], task["id"])["results"][0]
    assert result["output"]["answer"] == {"state": "withheld", "body": None, "reason": "sensitive"}
    assert read_receipt(case[0][0], case[0][1], task["id"]) == {
        "state": "available",
        "text": "这次处理已结束，但没有可展示的最终答复。可查看下方逐轮记录。",
    }
    assert not list(journal.path.parent.glob("*.env"))
    before = output.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Recovery must not re-extract or launch work")

    monkeypatch.setattr("milo_home.work_delivery.extract_output", forbidden)
    assert WorkRecovery(runtime).replay_results(case[0][1], window["id"]) == 1
    assert progress.results(case[0][1], task["id"])["results"] == [result]
    assert output.read_bytes() == before and len(fake_units[1]) == 1


def test_task_receipt_waits_for_late_result_and_replay_does_not_run_again(
    launch_case, fake_units, monkeypatch
):
    case, runtime, _, launcher, _, _ = launch_case
    database, owner, _, _, _, tasks, _ = case[0]
    task, window = reserve(case)
    with monkeypatch.context() as paused:
        paused.setattr(WorkRecovery, "replay_results", lambda *_: 0)
        launcher.run(owner, window["id"], credential="synthetic-secret")
    assert tasks.get(owner, task["id"])["status"] == "completed"
    assert read_receipt(database, owner, task["id"]) == {"state": "pending", "text": None}
    recovery = WorkRecovery(runtime)
    assert recovery.replay_results(owner, window["id"]) == 1
    answer = WorkProgress(database).results(owner, task["id"])["results"][0]["output"]["answer"]
    result = read_receipt(database, owner, task["id"])
    assert result["state"] == "available" and result["text"].endswith(answer["body"])
    assert recovery.replay_results(owner, window["id"]) == 1
    assert read_receipt(database, owner, task["id"]) == result
    assert len(fake_units[1]) == 1


def test_old_receipt_remains_readable_without_inventing_answer_or_counters(
    launch_case, fake_units, monkeypatch
):
    from test_work_migration import remove_reconciliation_schema

    case, runtime, journal, launcher, _, _ = launch_case
    task, window = reserve(case)
    original = journal.record

    def legacy(stream, event):
        return original(stream, {k: v for k, v in event.items() if k != "output"})

    monkeypatch.setattr(journal, "record", legacy)
    launcher.run(case[0][1], window["id"], credential="synthetic-secret")
    # Reconstruct the former schema around a genuinely executed legacy receipt.
    database = case[0][0]
    with database.connect(write=True) as connection:
        remove_reconciliation_schema(connection)
        connection.execute("ALTER TABLE work_results DROP COLUMN output")
        connection.execute("PRAGMA user_version=15")
        before = dict(connection.execute("SELECT * FROM work_results").fetchone())
        events = [dict(row) for row in connection.execute("SELECT * FROM work_events")]
    database.initialize()
    with database.connect() as connection:
        after = dict(connection.execute("SELECT * FROM work_results").fetchone())
        assert after.pop("output") is None and after == before
        assert [dict(row) for row in connection.execute("SELECT * FROM work_events")] == events
    result = WorkProgress(case[0][0]).results(case[0][1], task["id"])["results"][0]
    assert result["output"] == {
        "answer": {"state": "missing", "body": None, "reason": "not_recorded"},
        "actions": None,
    }
    assert WorkRecovery(runtime).replay_results(case[0][1], window["id"]) == 1

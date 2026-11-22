"""The sealed decision and its exact business projection must agree on replay and archival."""

from .reception_results import read_reception_projection, require_reception_projection
from .reply_choice_result import DeferChoice, ReplyChoice, ReplyChoiceResult
from .reply_results import require_scope
from .revisit_claims import read_claim
from .revisit_publication import choices_for_publication


def read_projection(connection, run_id):
    queries = {
        "decisions": "SELECT * FROM message_decisions WHERE run_id=? ORDER BY message_id",
        "coverage": "SELECT * FROM message_reply_links WHERE run_id=? ORDER BY reply_id,message_id",
        "revisits": "SELECT id,owner_id,conversation_id,origin_run_id,origin_index,initial_due_at,"
        "created_at FROM revisit_intents WHERE origin_run_id=? ORDER BY origin_index",
        "sources": "SELECT s.* FROM revisit_sources s JOIN revisit_intents r ON r.id=s.revisit_id "
        "WHERE r.origin_run_id=? ORDER BY s.revisit_id,s.message_id",
    }
    projection = {
        name: [dict(row) for row in connection.execute(query, (run_id,))]
        for name, query in queries.items()
    }
    reception = read_reception_projection(connection, run_id)
    if reception:
        projection["reception"] = reception
    saved = read_claim(connection, run_id)
    if saved is not None:
        projection["revisit_claim"] = saved
    return projection


def require_publication(result, run, published, details, message_ids, completed_at):
    saved = details.get("revisit_claim")
    sources = [row["message_id"] for row in saved["sources"]] if saved else []
    require_scope(result, message_ids, completed_at, revisit_source_ids=sources)
    if any(
        row["role"] != "milo"
        or row["thread_id"] != run["thread_id"]
        or row["conversation_id"] != run["conversation_id"]
        or row["run_id"] != run["id"]
        or row["author_id"] is not None
        or row["client_key"] is not None
        for row in published
    ):
        raise ValueError("Published replies have a different business scope")
    if not isinstance(result, ReplyChoiceResult):
        expected = {str(reply.message_id): reply.body for reply in result.replies}
        if (
            len(published) != len(expected)
            or {row["in_reply_to"]: row["body"] for row in published} != expected
        ):
            raise ValueError("Published replies differ from their sealed result")
        if any(details.values()):
            raise ValueError("Legacy publication contains unexpected decision projection")
        return
    _require_choices(result, run, published, details, completed_at)


def _require_choices(result, run, published, details, completed_at):
    require_reception_projection(result.reception, run, details.get("reception", []), completed_at)
    by_id = {row["id"]: row for row in published}
    groups = {}
    for row in details["coverage"]:
        if row["run_id"] != run["id"] or row["reply_id"] not in by_id:
            raise ValueError("Reply coverage differs from the sealed business run")
        groups.setdefault(row["reply_id"], []).append(row["message_id"])
    actual = {tuple(sorted(ids)): by_id[identity]["body"] for identity, ids in groups.items()}
    expected = {
        tuple(sorted(map(str, choice.message_ids))): choice.body
        for choice in result.decisions
        if isinstance(choice, ReplyChoice)
    }
    if actual != expected or len(published) != len(expected) or len(groups) != len(by_id):
        raise ValueError("Published answer groups differ from their sealed result")
    group_ids = {tuple(sorted(ids)): identity for identity, ids in groups.items()}
    visits = _require_revisits(result, run, details, completed_at)
    decisions = {row["message_id"]: row for row in details["decisions"]}
    choices = choices_for_publication(result, details.get("revisit_claim"))
    expected_ids = {str(value) for choice in choices for value in choice.message_ids}
    if len(decisions) != len(details["decisions"]) or set(decisions) != expected_ids:
        raise ValueError("Published decisions differ from the sealed input identities")
    for choice in choices:
        state = {"reply": "replied", "no_reply": "no_reply", "defer": "deferred"}[choice.kind]
        reply_id = (
            group_ids.get(tuple(sorted(map(str, choice.message_ids))))
            if state == "replied"
            else None
        )
        for value in choice.message_ids:
            row = decisions[str(value)]
            if (
                row["state"],
                row["reply_id"],
                row["revisit_id"],
                row["decided_at"],
                row["run_id"],
            ) != (state, reply_id, visits.get(str(value)), completed_at, run["id"]):
                raise ValueError("Published disposition differs from its sealed decision")


def _require_revisits(result, run, details, completed_at):
    requests = {
        index: item
        for index, item in enumerate([*result.decisions, *result.revisits])
        if isinstance(item, DeferChoice) or index >= len(result.decisions)
    }
    actual = {row["origin_index"]: row for row in details["revisits"]}
    if set(actual) != set(requests) or len(actual) != len(details["revisits"]):
        raise ValueError("Published revisits differ from the sealed requests")
    sources = {}
    for row in details["sources"]:
        sources.setdefault(row["revisit_id"], []).append(row["message_id"])
    if set(sources) != {row["id"] for row in actual.values()}:
        raise ValueError("Revisit source identities differ from the original projection")
    identities = {}
    for index, request in requests.items():
        row = actual[index]
        ids = set(map(str, request.message_ids))
        if (
            row["origin_run_id"] != run["id"]
            or row["conversation_id"] != run["conversation_id"]
            or row["created_at"] != completed_at
            or row["initial_due_at"] != request.revisit_at
            or set(sources[row["id"]]) != ids
        ):
            raise ValueError("Revisit birth evidence differs from the sealed request")
        identities.update({value: row["id"] for value in ids})
    return identities

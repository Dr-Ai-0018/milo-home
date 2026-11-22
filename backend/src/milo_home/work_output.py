"""Extract only exact-turn final answers before ephemeral credentials disappear.

This is a best-effort disclosure guard, not an arbitrary-secret detector. Reports
are sealed into the private terminal receipt; recovery never reinterprets text.
"""

import re
import tomllib
import unicodedata
from dataclasses import dataclass, field

from .work_output_models import ANSWER_LIMIT, WorkOutputView


@dataclass(frozen=True)
class OutputPolicy:
    sensitive: tuple[str, ...] = field(repr=False)

    @classmethod
    def for_runtime(cls, config, credential, home):
        source = tomllib.loads(config.content.decode())["model_providers"][config.provider]
        values = [credential, source["base_url"], str(home)]
        values.extend(source.get("http_headers", {}).values())
        return cls(tuple(value for value in values if value))

    def permits(self, text):
        normalized = unicodedata.normalize("NFKC", text)
        if any(unicodedata.normalize("NFKC", value) in normalized for value in self.sensitive):
            return False
        if any(unicodedata.category(c).startswith("C") and c not in "\n\t" for c in normalized):
            return False
        # Deliberately withhold the whole answer rather than silently editing it.
        return (
            re.search(
                r"(?i)(?:https?://[^\s]*[?&#](?:access_token|token|key|secret|signature)=|"
                r"\b(?:authorization|api[_-]?key|password|secret|access[_-]?token)\s*[:=]|"
                r"\b(?:sk|ghp|github_pat)[_-][a-z0-9_-]{12,}|"
                r"-----BEGIN [A-Z ]*PRIVATE KEY|"
                r"(?:^|[\s(\"'`])/(?:root|home|etc|var|run|opt|proc|sys|tmp)(?:/|\b)|"
                r"file://|[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@)",
                normalized,
            )
            is None
        )


def _items(entries, thread_id, turn_id):
    items = {}
    conflict = False
    for _, event in entries:
        if event.get("kind") != "notification":
            continue
        message = event.get("message", {})
        params = message.get("params", {})
        if (
            message.get("method") != "item/completed"
            or params.get("threadId") != thread_id
            or params.get("turnId") != turn_id
        ):
            continue
        item = params.get("item", {})
        if item.get("type") not in {"agentMessage", "commandExecution", "fileChange"}:
            continue
        identity = item.get("id")
        if not isinstance(identity, str) or not identity or len(identity) > 256:
            conflict = True
            continue
        if identity in items and items[identity] != item:
            conflict = True
        items[identity] = item
    return list(items.values()), conflict


def extract_output(entries, *, thread_id, turn_id, policy):
    items, conflict = _items(entries, thread_id, turn_id)
    finals = [
        i.get("text")
        for i in items
        if i["type"] == "agentMessage" and i.get("phase") == "final_answer"
    ]
    answer = {"state": "missing", "body": None, "reason": "no_final_answer"}
    reason = None
    if conflict or len(finals) > 1 or (finals and not isinstance(finals[0], str)):
        reason = "ambiguous"
    elif finals and finals[0].strip():
        body = finals[0].strip()
        if len(body) > ANSWER_LIMIT:
            reason = "too_long"
        elif not policy.permits(body):
            reason = "sensitive"
        else:
            answer = {"state": "available", "body": body, "reason": None}
    if reason:
        answer = {"state": "withheld", "body": None, "reason": reason}
    actions = (
        None
        if conflict
        else {
            "command_calls": sum(i["type"] == "commandExecution" for i in items),
            "file_change_calls": sum(i["type"] == "fileChange" for i in items),
        }
    )
    if actions is not None and max(actions.values()) > 10000:
        actions = None
    return WorkOutputView(answer=answer, actions=actions).model_dump()

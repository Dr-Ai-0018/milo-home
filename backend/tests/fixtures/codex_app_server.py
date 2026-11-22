"""Synthetic stdio peer; never invokes Codex or a model provider."""

import json
import os
import sys
import time
from pathlib import Path

scenario, output = sys.argv[1:]
turns = 0
thread_id = "main-thread"
workspace = None


def send(message):
    data = (json.dumps(message, ensure_ascii=False) + "\n").encode()
    if scenario == "split":
        for byte in data:
            os.write(1, bytes([byte]))
    else:
        os.write(1, data)


def event(method, **params):
    send({"method": method, "params": {"threadId": thread_id, **params}})


for line in sys.stdin:
    request = json.loads(line)
    with Path(output).open("a") as stream:
        stream.write(json.dumps(request, ensure_ascii=False) + "\n")
    method = request["method"]
    if "id" not in request:
        continue
    result = {}
    if method == "thread/resume":
        if scenario.startswith("auto-"):
            send(
                {"method": "configWarning", "params": {"summary": "Synthetic compatibility notice"}}
            )
        thread_id = request["params"]["threadId"]
        workspace = Path(request["params"]["cwd"])
        if scenario == "resume-eof":
            sys.exit(0)
        result = {
            "thread": {"id": request["params"]["threadId"], "status": {"type": "idle"}},
            "model": request["params"].get("model", "synthetic-model"),
            "modelProvider": "synthetic-provider",
            "cwd": request["params"]["cwd"],
            "sandbox": {"type": "dangerFullAccess"},
            "approvalPolicy": "never",
        }
        if scenario == "wrong-provider":
            result["modelProvider"] = "fallback"
        if scenario == "wrong-sandbox":
            result["sandbox"] = {"type": "readOnly"}
        if scenario == "active":
            result["thread"]["status"] = {"type": "active"}
        if scenario.startswith("auto-"):
            result["sandbox"] = {"type": "workspaceWrite"}
            result["approvalPolicy"] = "on-request"
            result["approvalsReviewer"] = "auto_review"
            if scenario == "auto-human":
                result["approvalsReviewer"] = "user"
            if scenario == "auto-never":
                result["approvalPolicy"] = "never"
            if scenario == "auto-unrestricted":
                result["sandbox"] = {"type": "dangerFullAccess"}
            if scenario == "auto-reviewer-missing":
                del result["approvalsReviewer"]
    if method == "turn/start":
        turns += 1
        turn = {"id": "turn-" + str(turns), "status": "inProgress"}
        reply_text = None
        if scenario.startswith("auto-work-"):
            turn["id"] = "work-" + request["params"]["clientUserMessageId"]
            payload = json.loads(request["params"]["input"][0]["text"].rsplit("\n", 1)[-1])
            decision = {
                "auto-work-deferred": "deferred",
                "auto-work-declined": "declined",
                "auto-work-clarify": "needs_clarification",
            }.get(scenario, "accepted")
            reply_text = json.dumps(
                {
                    "version": 1,
                    "decision": decision,
                    "receipt": "合成工作回执",
                    "revisit_at": payload["observed_at"] + 60 if decision == "deferred" else None,
                    "reception": {
                        "scope": "all",
                        "mode": "resting",
                        "resume_on": "explicit",
                        "until": None,
                    }
                    if scenario == "auto-work-reception"
                    else None,
                },
                ensure_ascii=False,
            )
            if scenario == "auto-work-invalid":
                reply_text = "not a structured work choice"
            if scenario == "auto-work-sensitive":
                reply_text = reply_text.replace("合成工作回执", "synthetic-private-file-key")
        if scenario.startswith("auto-reply"):
            turn["id"] = "native-" + request["params"]["clientUserMessageId"]
            payload = json.loads(request["params"]["input"][0]["text"].rsplit("\n", 1)[-1])
            reply_text = json.dumps(
                {
                    "replies": [
                        {"message_id": item["id"], "body": "合成原生回信。"}
                        for item in payload["selected"]
                    ]
                },
                ensure_ascii=False,
            )
            if "decisions" in request["params"].get("outputSchema", {}).get("properties", {}):
                ids = [item["id"] for item in payload["selected"]]
                revisit_ids = payload.get("revisit", {}).get("source_ids", [])
                decisions = [
                    {"kind": "reply", "message_ids": [value], "body": "合成原生回信。"}
                    for value in ids
                ]
                if scenario == "auto-reply-merged":
                    decisions = [{"kind": "reply", "message_ids": ids, "body": "合成原生合回。"}]
                if scenario == "auto-reply-no-reply":
                    decisions = [{"kind": "no_reply", "message_ids": ids}]
                if scenario == "auto-reply-defer":
                    decisions = [
                        {
                            "kind": "defer",
                            "message_ids": ids + revisit_ids,
                            "revisit_at": payload["created_at"] + 3600,
                        }
                    ]
                if scenario == "auto-reply-mixed":
                    decisions = [
                        {"kind": "reply", "message_ids": ids[:2], "body": "合成原生合回。"},
                        {"kind": "no_reply", "message_ids": ids[2:3]},
                        {
                            "kind": "defer",
                            "message_ids": ids[3:],
                            "revisit_at": payload["created_at"] + 3600,
                        },
                    ]
                reception = None
                revisits = []
                if scenario == "auto-reply-future-supplement":
                    decisions = [{"kind": "reply", "message_ids": ids, "body": "合成先回这一句。"}]
                    revisits = [{"message_ids": ids, "revisit_at": payload["created_at"] + 3600}]
                if scenario == "auto-reply-followup":
                    decisions = [
                        {
                            "kind": "reply",
                            "message_ids": ids + revisit_ids,
                            "body": "合成到期补话。",
                        }
                    ]
                if scenario == "auto-reply-quiet-revisit":
                    decisions = [{"kind": "no_reply", "message_ids": ids}] if ids else []
                if scenario == "auto-reply-reception":
                    reception = {
                        "scope": "all",
                        "mode": "resting",
                        "resume_on": "explicit",
                        "until": None,
                    }
                reply_text = json.dumps(
                    {
                        "version": 2,
                        "decisions": decisions,
                        "revisits": revisits,
                        "reception": reception,
                    },
                    ensure_ascii=False,
                )
            if scenario == "auto-reply-invalid":
                reply_text = "Not a valid structured final answer"
            if scenario == "auto-reply-sensitive":
                reply_text = reply_text.replace("合成原生回信。", "synthetic-private-file-key")
        if scenario == "error-before-start":
            event("error", turnId=turn["id"], willRetry=False, error={"message": "synthetic"})
        event("turn/started", turn=turn)
        if scenario == "auto-reply-lost":
            time.sleep(60)
        if scenario == "auto-reply-wait":
            send({"id": request["id"], "result": {"turn": turn}})
            time.sleep(60)
        if scenario in {"auto-normal", "auto-foreign-review", "auto-foreign-strict"}:
            event("guardianWarning", message="Synthetic native review decision")
            event(
                "autoApprovalReview/strictReviewRequired",
                turnId="foreign" if scenario == "auto-foreign-strict" else turn["id"],
                startedAtMs=1,
            )
            for phase in ("started", "completed"):
                event(
                    "item/autoApprovalReview/" + phase,
                    turnId="foreign" if scenario == "auto-foreign-review" else turn["id"],
                    review={"status": "approved"},
                )
            event("item/completed", turnId=turn["id"], item={"type": "contextCompaction"})
            event("thread/compacted")
        if scenario == "auto-manual-request":
            send({"id": 999, "method": "item/commandExecution/requestApproval", "params": {}})
            time.sleep(60)
        if scenario in {
            "error-then-failed",
            "error-no-terminal",
            "error-foreign-turn",
            "error-retry",
        }:
            send({"id": request["id"], "result": {"turn": turn}})
            event(
                "error",
                turnId="foreign-turn" if scenario == "error-foreign-turn" else turn["id"],
                willRetry=scenario == "error-retry",
                error={"message": "synthetic provider error"},
            )
            if scenario == "error-no-terminal":
                sys.exit(0)
            status = "completed" if scenario == "error-retry" else "failed"
            event("turn/completed", turn={"id": turn["id"], "status": status})
            continue
        if scenario == "eof":
            sys.exit(0)
        if scenario == "lost":
            time.sleep(60)
        if scenario == "malformed":
            os.write(1, b"[]\n")
            time.sleep(60)
        if scenario == "frame-limit":
            os.write(1, b"x" * (256 * 1024 + 1))
            time.sleep(60)
        if scenario == "stderr-limit":
            os.write(2, b"x" * (2 * 1024 * 1024 + 1))
            time.sleep(60)
        if scenario == "server-request":
            send({"id": 999, "method": "item/commandExecution/requestApproval", "params": {}})
            time.sleep(60)
        if scenario == "unknown-item":
            event("item/started", item={"type": "futureExecutionTool"})
        if scenario in {
            "deliver-file",
            "deliver-sensitive",
            "deliver-link",
            "attachment",
            "attachment-secret",
        }:
            prompt = request["params"]["input"][0]["text"]
            task = json.loads(next(line for line in prompt.splitlines() if line.startswith("{")))[
                "task"
            ]
            path = workspace / task["deliverable_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            if scenario in {"attachment", "attachment-secret"}:
                target = workspace / task["handoff_path"] / "sample.bin"
                target.write_bytes(
                    b"\x00synthetic-secret" if scenario == "attachment-secret" else b"\x00\xff"
                )
            if scenario == "deliver-link":
                other = workspace / "private.txt"
                other.write_text("private sibling", encoding="utf-8")
                path.symlink_to(other)
            else:
                content = (
                    "synthetic-secret" if scenario == "deliver-sensitive" else "实际交回的内容。\n"
                )
                path.write_text(content, encoding="utf-8")
        if not scenario.startswith("steer-") and scenario not in {"interrupt", "unknown-item"}:
            event("item/started", item={"type": "commandExecution", "id": "tool"})
            event(
                "item/completed", turnId=turn["id"], item={"type": "commandExecution", "id": "tool"}
            )
            event(
                "item/completed",
                turnId=turn["id"],
                item={
                    "type": "agentMessage",
                    "id": "answer",
                    "phase": "final_answer",
                    "text": reply_text
                    or ("synthetic-secret" if scenario == "secret-answer" else "合成结果"),
                },
            )
            status = {
                "failed-terminal": "failed",
                "error-before-start": "failed",
                "interrupted-terminal": "interrupted",
            }.get(scenario, "completed")
            event("turn/completed", turn={"id": turn["id"], "status": status})
        result = {"turn": turn}
    if method == "turn/steer" and scenario == "steer-conflict":
        event("turn/completed", turn={"id": "turn-1", "status": "completed"})
        send({"id": request["id"], "error": {"code": -32600, "message": "private-details"}})
        continue
    if method == "turn/steer" and scenario == "steer-lost":
        time.sleep(60)
    if method == "turn/steer" and scenario in {"steer-accepted", "steer-wrong-turn"}:
        result = {"turnId": "turn-1" if scenario == "steer-accepted" else "another-turn"}
        event("turn/completed", turn={"id": "turn-1", "status": "completed"})
    if method == "turn/interrupt":
        event("turn/completed", turn={"id": "turn-1", "status": "interrupted"})
    send({"id": request["id"], "result": result})

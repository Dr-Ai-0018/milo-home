"""Loopback-only mock for R10 page development; no database or model."""

import argparse
import hashlib
import io
import json
import time
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID


def identity(number):
    return str(UUID(int=number))


USER = {"id": identity(1), "handle": "preview", "display_name": "合成预览"}
WORKSPACE = {"id": identity(2), "name": "合成交办桌"}
SESSION = {
    "id": identity(3),
    "workspace_id": WORKSPACE["id"],
    "created_at": 1794556800,
    "execution_available": False,
}
FILES = {
    identity(401): ("summary.md", "# 合成成果\n\n只用于页面预览。\n".encode()),
    identity(402): ("counts.csv", b"item,count\nexample,2\n"),
}
SAMPLES = (
    ("running", "正在做", "pending", None, "pending"),
    ("completed", "纯执行", "available", "已检查样例目录，未更改文件。", "none"),
    ("completed", "两份文件", "available", "已交付一份说明和一份统计表。", "available"),
    ("failed", "执行失败", "available", "读取样例时失败；尚未生成报告。", "none"),
    ("cancelled", "已取消", "available", "已停下；报告未生成。", "none"),
    ("timed_out", "已超时", "available", "超过执行期限；没有可核对的成果。", "none"),
    (
        "needs_reconciliation",
        "待核对",
        "available",
        "没有足够证据确认结果。",
        "pending",
    ),
    ("completed", "回执晚到", "pending", None, "none"),
    ("completed", "附件暂时读不到", "available", "回执可读，附件请稍后重读。", "error"),
    ("completed", "旧 TXT", "unavailable", None, "none"),
    (
        "completed",
        "附件未能封存",
        "available",
        "任务结束，未能封存约定的附件。",
        "unavailable",
    ),
)


def scenario(index, sample):
    status, title, receipt_state, text, attachment_state = sample
    task_id = identity(100 + index)
    input_id = identity(200 + index)
    now = SESSION["created_at"] + index * 60
    initial = {
        "id": input_id,
        "task_id": task_id,
        "sequence": 1,
        "kind": "initial",
        "body": f"合成样例：{title}",
        "delivery": "queued" if status == "queued" else "acknowledged",
        "created_at": now,
        "active_turn": None,
    }
    legacy = title == "旧 TXT"
    legacy_path = f"deliverables/{task_id}/note.txt"
    task = {
        "id": task_id,
        "session_id": SESSION["id"],
        "sequence": index,
        "status": status,
        "created_at": now,
        "updated_at": now,
        "stop_requested_at": now if status in {"stopping", "cancelled"} else None,
        "initial_input": initial,
        "deliverable": {"filename": "note.txt", "path": legacy_path, "state": "saved"}
        if legacy
        else None,
        "active_turn": identity(300 + index) if status == "running" else None,
        "execution_available": False,
    }
    receipt = {"state": receipt_state, "text": text}
    attachments = {
        "state": attachment_state,
        "attachments": [
            {
                "id": file_id,
                "filename": filename,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for file_id, (filename, data) in FILES.items()
            if attachment_state == "available"
        ],
    }
    legacy_text = "旧版 TXT 合成成果。"
    artifacts = (
        [
            {
                "path": legacy_path,
                "text": legacy_text,
                "sha256": hashlib.sha256(legacy_text.encode()).hexdigest(),
                "checked_at": now,
            }
        ]
        if legacy
        else []
    )
    event = {
        "task_id": task_id,
        "sequence": 1,
        "kind": "task_status",
        "state": status,
        "input_id": None,
        "result_id": None,
        "recorded_at": now,
    }
    results = []
    if status == "completed" and (legacy or receipt_state == "available"):
        results.append(
            {
                "id": identity(500 + index),
                "task_id": task_id,
                "input_id": input_id,
                "outcome": "completed",
                "observed_at": now,
                "output": {
                    "answer": {
                        "state": "available",
                        "body": "这是合成逐轮答复。",
                        "reason": None,
                    },
                    "actions": None,
                },
            }
        )
    return {
        "": task,
        "inputs": {"inputs": [initial], "next_after": 1},
        "results": {"results": results},
        "artifacts": {"artifacts": artifacts},
        "events": {"events": [event], "next_after": 1, "has_more": False},
        "receipt": receipt,
        "attachments": attachments,
    }


CASES = {
    identity(100 + index): scenario(index, sample)
    for index, sample in enumerate(SAMPLES, start=1)
}
TASK_LIST = f"/api/work-sessions/{SESSION['id']}/tasks"
TASK_HISTORY = f"/api/work-sessions/{SESSION['id']}/task-history"
GROUPS = {
    "all": None,
    "active": {"queued", "running", "stopping"},
    "needs_reconciliation": {"needs_reconciliation"},
    "finished": {"completed", "failed", "cancelled", "timed_out"},
}


def preview_cases(large_history):
    cases = dict(CASES)
    if large_history:
        for sequence in range(12, 1002):
            sample = ("completed", f"历史单 {sequence}", "unavailable", None, "none")
            cases[identity(100 + sequence)] = scenario(sequence, sample)
    newest = 1002 if large_history else 12
    for index, (status, title) in enumerate(
        (("queued", "排队"), ("stopping", "正在停下")), newest
    ):
        cases[identity(100 + index)] = scenario(
            index, (status, title, "pending", None, "none")
        )
    return cases


STATIC = {
    "/api/auth/me": USER,
    "/api/workspaces": [WORKSPACE],
    f"/api/workspaces/{WORKSPACE['id']}/main-session": SESSION,
    f"/api/work-sessions/{SESSION['id']}": SESSION,
}


class Preview(BaseHTTPRequestHandler):
    def reply(
        self,
        data,
        status=HTTPStatus.OK,
        *,
        content_type="application/json",
        filename=None,
    ):
        if content_type == "application/json":
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
        self.end_headers()
        self.wfile.write(data)

    def error(self, status, code):
        self.reply({"error": {"code": code, "message": "合成预览响应"}}, status)

    def task_route(self, path):
        prefix = "/api/work-tasks/"
        if path.startswith(prefix):
            task_id, _, operation = path[len(prefix) :].partition("/")
            return self.server.cases.get(task_id), operation
        return None, ""

    def do_GET(self):
        url = urlsplit(self.path)
        path = unquote(url.path)
        if path in STATIC:
            return self.reply(STATIC[path])
        if path == TASK_LIST:
            return self.list_tasks(parse_qs(url.query, keep_blank_values=True))
        if path == TASK_HISTORY:
            return self.task_history(parse_qs(url.query, keep_blank_values=True))
        case, operation = self.task_route(path)
        if case is None:
            return self.error(404, "work_not_found")
        if operation == "attachments" and case["attachments"]["state"] == "error":
            return self.error(503, "storage_error")
        if operation in case:
            if operation == "receipt" and case[""]["id"] == identity(108):
                return self.late_receipt()
            return self.reply(case[operation])
        if operation.startswith("attachments/") and operation.endswith("/download"):
            parts = operation.split("/")
            if len(parts) == 3 and parts[1] in self.file_ids(case):
                filename, data = FILES[parts[1]]
                return self.reply(
                    data, content_type="application/octet-stream", filename=filename
                )
        self.error(404, "work_not_found")

    def list_tasks(self, query):
        try:
            after = int(query.get("after", [0])[-1])
            limit = int(query.get("limit", [20])[-1])
            if after < 0 or not 1 <= limit <= 50:
                raise ValueError
        except ValueError:
            return self.error(422, "invalid_input")
        tasks = [
            case[""]
            for case in self.server.cases.values()
            if case[""]["sequence"] > after
        ][:limit]
        return self.reply(
            {"tasks": tasks, "next_after": tasks[-1]["sequence"] if tasks else after}
        )

    def task_history(self, query):
        group = query.get("status_group", ["all"])[-1]
        try:
            before = int(query["before"][-1]) if "before" in query else None
            limit = int(query.get("limit", [20])[-1])
            if group not in GROUPS or not 1 <= limit <= 50:
                raise ValueError
            if before is not None and not 1 <= before <= 2**63 - 1:
                raise ValueError
        except ValueError:
            return self.error(422, "invalid_input")
        if self.server.history_unavailable:
            return self.error(404, "work_not_found")
        if group == self.server.slow_group:
            time.sleep(1.5)
        statuses = GROUPS[group]
        tasks = [
            case[""]
            for case in reversed(tuple(self.server.cases.values()))
            if (before is None or case[""]["sequence"] < before)
            and (statuses is None or case[""]["status"] in statuses)
        ][: limit + 1]
        return self.reply(
            {
                "tasks": tasks[:limit],
                "next_before": tasks[limit - 1]["sequence"]
                if len(tasks) > limit
                else None,
            }
        )

    def late_receipt(self):
        # First fetch models the terminal-state/receipt race; reloading or polling can recover it.
        with self.server.receipt_lock:
            reads = self.server.late_reads
            self.server.late_reads += 1
        if reads == 0:
            return self.reply({"state": "pending", "text": None})
        return self.reply(
            {"state": "available", "text": "已完成样例核对；回执稍后补到。"}
        )

    def file_ids(self, case):
        return [item["id"] for item in case["attachments"]["attachments"]]

    def do_PUT(self):
        path = urlsplit(self.path).path
        if path == f"/api/workspaces/{WORKSPACE['id']}/main-session":
            return self.reply(SESSION)
        self.error(404, "work_not_found")

    def do_POST(self):
        case, operation = self.task_route(urlsplit(self.path).path)
        if case is None or operation != "attachments/archive":
            return self.error(405, "preview_read_only")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 4096:
                raise ValueError
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict) or set(request) != {"attachment_ids"}:
                raise ValueError
            selected = request["attachment_ids"]
            available = self.file_ids(case)
            if selected is None:
                selected = available
            if (
                not isinstance(selected, list)
                or not selected
                or not all(isinstance(value, str) for value in selected)
                or len(set(selected)) != len(selected)
            ):
                raise ValueError
        except (ValueError, UnicodeError):
            return self.error(422, "invalid_input")
        if any(value not in available for value in selected):
            return self.error(404, "work_not_found")
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
            for file_id in selected:
                filename, data = FILES[file_id]
                archive.writestr(filename, data)
        self.reply(
            payload.getvalue(), content_type="application/zip", filename="preview.zip"
        )


def main():
    from threading import Lock

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=17484)
    parser.add_argument("--large-history", action="store_true")
    parser.add_argument("--slow-group", choices=GROUPS)
    parser.add_argument("--history-unavailable", action="store_true")
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Preview) as server:
        server.late_reads = 0
        server.receipt_lock = Lock()
        server.cases = preview_cases(args.large_history)
        server.slow_group = args.slow_group
        server.history_unavailable = args.history_unavailable
        print(f"合成交办预览 API：http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

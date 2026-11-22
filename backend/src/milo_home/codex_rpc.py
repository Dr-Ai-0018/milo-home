"""Bounded synchronous stdio RPC; callers own durable records and execution policy."""

import json
import os
import selectors
import threading
import time
from collections.abc import Callable

METHODS = {
    "initialize",
    "thread/start",
    "thread/inject_items",
    "thread/resume",
    "thread/read",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
}


class ProtocolError(RuntimeError):
    pass


class DeliveryUnknown(RuntimeError):
    """The request may have reached Codex; never retry it automatically."""


class RequestRejected(RuntimeError):
    pass


class StdioRpc:
    def __init__(
        self, process, *, deadline: float, record: Callable, stop: Callable, checkpoint: Callable
    ):
        self.process = process
        self.writer = threading.get_ident()
        if not 0 < deadline - time.monotonic() <= 3600:
            raise ValueError("A finite execution deadline is required")
        self.deadline = deadline
        self.record = record
        self.stop_process = stop
        self.checkpoint = checkpoint
        self.on_notification = lambda message: None
        self.selector = selectors.DefaultSelector()
        for pipe, name in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
            os.set_blocking(pipe.fileno(), False)
            self.selector.register(pipe, selectors.EVENT_READ, name)
        os.set_blocking(process.stdin.fileno(), False)
        self.buffer = b""
        self.total = 0
        self.sequence = 0
        self.pending = None
        self.response = None
        self.broken = False
        self.closed = False

    def request(self, method: str, params: dict, *, timeout: float = 10) -> dict:
        self.assert_writer()
        if method not in METHODS or not isinstance(params, dict) or timeout <= 0:
            raise ValueError("Unsupported request")
        if self.broken or self.closed or self.pending is not None:
            raise ProtocolError("Connection unavailable")
        self.sequence += 1
        request = {"id": self.sequence, "method": method, "params": params}
        data = self._encode(request)
        self.record({"kind": "intent", "request": request})
        self.pending = self.sequence
        end = min(self.deadline, time.monotonic() + timeout)
        try:
            self._write(data, end)
            while self.response is None:
                self.pump(until=end)
            response = self.response
            self.record({"kind": "response", "id": self.pending, "response": response})
        except Exception as error:
            self.broken = True
            # Record failure must not make the delivery appear safe to replay.
            try:
                self.record({"kind": "unknown", "id": self.pending})
            finally:
                raise DeliveryUnknown("Request delivery requires reconciliation") from error
        finally:
            self.pending = None
            self.response = None
        if "error" in response:
            raise RequestRejected("Codex rejected the request")
        return response["result"]

    def notify_initialized(self):
        self.assert_writer()
        if self.broken or self.closed or self.pending is not None:
            raise ProtocolError("Connection unavailable")
        try:
            self._write(self._encode({"method": "initialized"}), self.deadline)
        except Exception:
            self.broken = True
            raise

    def _encode(self, message: dict) -> bytes:
        try:
            data = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode()
        except (ValueError, TypeError, UnicodeError) as error:
            raise ValueError("Invalid protocol input") from error
        if len(data) > 256 * 1024:
            raise ValueError("Protocol input too large")
        return data

    def _write(self, data: bytes, until: float):
        with selectors.DefaultSelector() as writable:
            writable.register(self.process.stdin, selectors.EVENT_WRITE)
            while data:
                self.checkpoint()
                remaining = min(until, self.deadline) - time.monotonic()
                if remaining <= 0:
                    raise ProtocolError("Write deadline")
                if writable.select(min(remaining, 0.1)):
                    try:
                        data = data[os.write(self.process.stdin.fileno(), data) :]
                    except BlockingIOError:
                        continue

    def pump(self, *, until: float | None = None):
        self.assert_writer()
        if self.broken or self.closed:
            raise ProtocolError("Connection unavailable")
        self.checkpoint()
        end = self.deadline if until is None else min(until, self.deadline)
        remaining = end - time.monotonic()
        if remaining <= 0:
            self.broken = True
            raise ProtocolError("Read deadline")
        try:
            for key, _ in self.selector.select(min(remaining, 0.1)):
                try:
                    data = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not data:
                    self.selector.unregister(key.fileobj)
                    if key.data == "stdout":
                        raise ProtocolError("Protocol stream ended")
                    continue
                self.total += len(data)
                if self.total > 2 * 1024 * 1024:
                    raise ProtocolError("Protocol output limit")
                if key.data == "stdout":
                    self._feed(data)
        except Exception:
            self.broken = True
            raise

    def _feed(self, data: bytes):
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if len(line) > 256 * 1024:
                raise ProtocolError("Protocol frame too large")
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError) as error:
                raise ProtocolError("Invalid protocol frame") from error
            if not isinstance(message, dict):
                raise ProtocolError("Invalid protocol message")
            if "method" in message:
                if "id" in message or not isinstance(message["method"], str):
                    raise ProtocolError("Unsupported server request")
                if not isinstance(message.get("params", {}), dict):
                    raise ProtocolError("Invalid notification parameters")
                self.record({"kind": "notification", "message": message})
                self.on_notification(message)
            else:
                valid_id = type(message.get("id")) is int and message["id"] == self.pending
                valid_result = isinstance(message.get("result"), dict)
                valid_error = isinstance(message.get("error"), dict)
                if not valid_id or valid_result == valid_error or self.response is not None:
                    raise ProtocolError("Unexpected protocol response")
                self.response = message
        if len(self.buffer) > 256 * 1024:
            raise ProtocolError("Protocol frame too large")

    def close(self) -> bool:
        self.assert_writer()
        if self.closed:
            return self.ended
        self.closed = True
        self.ended = False
        try:
            self.ended = bool(self.stop_process())
            return self.ended
        finally:
            for pipe in [self.process.stdin, self.process.stdout, self.process.stderr]:
                pipe.close()
            self.selector.close()

    def assert_writer(self):
        if threading.get_ident() != self.writer:
            raise ProtocolError("Connection belongs to another writer")

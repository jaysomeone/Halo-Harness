"""tests.helpers.mock_databricks -- a path-dispatched Databricks mock (H1):
serves both namespaces (`/serving-endpoints/<name>/invocations`,
`/ai-gateway/mlflow/v1/chat/completions`), enforces the strict body
allowlist (an unknown field -> 400 `json: unknown field "X"`, matching
Databricks' real wording), and can reply with either reasoning shape
(top-level `reasoning_content` deltas, or `{"type":"reasoning","summary":[...]}`
content-list blocks) plus a 429 body carrying `retry_after`/`limit_type`.

Reuses the same hand-rolled chunked-SSE plumbing as mock_openai.py (own
copy, not imported, so this file has no dependency on it beyond that
shared shape).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from rolo_claude.providers.profiles import DATABRICKS_BODY_ALLOWLIST

_ALLOWED_KEYS = DATABRICKS_BODY_ALLOWLIST | {"model"}


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _write(handler, data: bytes) -> None:
    try:
        handler.wfile.write(data)
        handler.wfile.flush()
    except OSError:
        pass


def _send_json(handler, status: int, obj, extra_headers=None) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "keep-alive")
    for k, v in (extra_headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    _write(handler, body)


def _start_sse(handler, status: int = 200) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()


def _sse_chunk(handler, data) -> None:
    frame = b"data: " + (b"[DONE]" if data == "[DONE]" else json.dumps(data, ensure_ascii=False).encode("utf-8")) + b"\r\n\r\n"
    _write(handler, b"%x\r\n" % len(frame) + frame + b"\r\n")


def _end_sse(handler) -> None:
    try:
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()
    except OSError:
        pass


def _finish(handler, chunks: list) -> None:
    _start_sse(handler)
    for c in chunks:
        _sse_chunk(handler, c)
    _sse_chunk(handler, "[DONE]")
    _end_sse(handler)


def _scn_reasoning_content_shape(handler, body):
    """Shape 1: top-level `reasoning_content` deltas (DeepSeek/Kimi/GLM/Grok)."""
    _finish(handler, [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "thinking step 1"}}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": " step 2"}}]},
        {"choices": [{"index": 0, "delta": {"content": "the answer"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


def _scn_reasoning_blocks_shape(handler, body):
    """Shape 2: `{"type":"reasoning","summary":[...]}` content-list blocks (Claude/GPT/Gemini on Databricks)."""
    _finish(handler, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "block reasoning text"}]},
        ]}}]},
        {"choices": [{"index": 0, "delta": {"content": [{"type": "text", "text": "final answer"}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


def _scn_rate_limit_429(handler, body):
    _send_json(handler, 429, {"error": {
        "message": "Rate limit exceeded: too many requests", "type": "rate_limit_exceeded", "code": 429,
        "limit_type": "input_tokens_per_minute", "limit": 200000, "current": 200150, "retry_after": 2,
    }})


def _scn_ok(handler, body):
    _finish(handler, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": "ok"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


SCENARIOS = {
    "reasoning-content-shape": _scn_reasoning_content_shape,
    "reasoning-blocks-shape": _scn_reasoning_blocks_shape,
    "rate-limit-429": _scn_rate_limit_429,
    "ok": _scn_ok,
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except (ValueError, TypeError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            body = {}

        mock: MockDatabricks = self.server.mock  # type: ignore[attr-defined]
        with mock._lock:
            mock.requests.append({"path": self.path, "body": body})

        # Strict allowlist guard (scope B/D): ANY key outside the allowlist -> 400,
        # matching Databricks' real "json: unknown field \"X\"" wording.
        unknown = [k for k in body if k not in _ALLOWED_KEYS]
        if unknown:
            _send_json(self, 400, {"error_code": "BAD_REQUEST", "message": f'json: unknown field "{unknown[0]}"'})
            return

        # Scenario name travels as a SUFFIX of the model name (mlflow/v1
        # route body) or the invocations path segment -- e.g. a test model
        # "databricks-kimi-k3-reasoning-content-shape" dispatches to the
        # "reasoning-content-shape" scenario. Longest-suffix match so e.g.
        # "ok" doesn't shadow a more specific name that also ends in it.
        model = body.get("model", "") or ""
        if model:
            candidate = model
        elif "/serving-endpoints/" in self.path:
            # /serving-endpoints/<model>/invocations -- the invocations
            # route's body has NO "model" field at all (real Databricks
            # shape), so the scenario name has to come from the URL here.
            candidate = self.path.split("/serving-endpoints/", 1)[1].split("/", 1)[0]
        else:
            candidate = self.path.rsplit("/", 1)[-1]
        scenario = "ok"
        for name in sorted(SCENARIOS, key=len, reverse=True):
            if candidate.endswith(name):
                scenario = name
                break
        fn = SCENARIOS.get(scenario, _scn_ok)
        fn(self, body)

    def do_GET(self):
        _send_json(self, 404, {"error": "mock databricks only serves POST"})


class MockDatabricks:
    def __init__(self):
        self._server = None
        self._thread = None
        self.requests: list = []
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def root(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "MockDatabricks":
        self._server = _ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.mock = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._server.server_close()
        except Exception:
            pass
        self._server = None

    def __enter__(self) -> "MockDatabricks":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

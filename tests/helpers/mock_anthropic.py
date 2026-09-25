"""tests.helpers.mock_anthropic -- H5 scope C/test-list: a path-dispatched
native-Anthropic-Messages mock. Serves `/v1/messages` (the `ant:` direct
route) AND both Databricks Claude-passthrough paths (`/ai-gateway/
anthropic/v1/messages`, `/serving-endpoints/anthropic/v1/messages`) so the
SAME mock backs both `stream.stream_anthropic_completion` call shapes
(`call_anthropic_native`'s two `route_provider` branches).

Reuses the same hand-rolled chunked-SSE plumbing as mock_databricks.py (own
copy, no import dependency), emitting REAL Anthropic event shapes
(message_start/content_block_start/content_block_delta/content_block_stop/
message_delta/message_stop/ping) rather than OpenAI chat-completion chunks.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


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


def _sse_event(handler, event: dict) -> None:
    frame = f"event: {event['type']}\r\ndata: {json.dumps(event, ensure_ascii=False)}\r\n\r\n".encode("utf-8")
    _write(handler, b"%x\r\n" % len(frame) + frame + b"\r\n")


def _end_sse(handler) -> None:
    try:
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()
    except OSError:
        pass


def _finish(handler, events: list) -> None:
    _start_sse(handler)
    for ev in events:
        _sse_event(handler, ev)
    _end_sse(handler)


def _usage(input_tokens=100, output_tokens=20, cache_read=0, cache_write=0) -> dict:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_write, "cache_read_input_tokens": cache_read}


def _scn_ok(h, body):
    _finish(h, [
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
         "content": [], "model": body.get("model", "claude"), "usage": _usage(output_tokens=0)}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "pong"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": _usage()},
        {"type": "message_stop"},
    ])


def _scn_thinking_and_signature(h, body):
    """Thinking block WITH its `signature` + cache usage fields -- the
    acceptance scenario's "thinking shown dimmed and cache fields in
    usage"."""
    _finish(h, [
        {"type": "message_start", "message": {"id": "msg_2", "type": "message", "role": "assistant",
         "content": [], "model": body.get("model", "claude"), "usage": _usage(output_tokens=0)}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "let me consider this"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig_abc123"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "pong"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": _usage(input_tokens=500, output_tokens=30, cache_read=200, cache_write=50)},
        {"type": "message_stop"},
    ])


def _scn_tool_use(h, body):
    """`input_json_delta` streamed in pieces, per the test list."""
    _finish(h, [
        {"type": "message_start", "message": {"id": "msg_3", "type": "message", "role": "assistant",
         "content": [], "model": body.get("model", "claude"), "usage": _usage(output_tokens=0)}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "toolu_01", "name": "Read", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"file_path"'}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": ':"/x.py"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": _usage()},
        {"type": "message_stop"},
    ])


def _scn_thinking_then_tool_then_reply(h, body):
    """finding 17 (test-quality): the one item on its own 8-case list with
    NO coverage anywhere else -- a real two-request `ant:`/Databricks-
    passthrough turn (thinking + tool_use, then a plain final reply after
    the tool result comes back) driven through a REAL `Session`, not a
    provider-layer helper called directly. Dispatches on whether the
    INCOMING request already carries a `tool_result` block (request 2)
    rather than on call count, so it works no matter how many times a
    retry ladder might re-send request 1."""
    has_tool_result = any(
        isinstance(m.get("content"), list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"]
        )
        for m in (body.get("messages") or [])
    )
    if has_tool_result:
        _finish(h, [
            {"type": "message_start", "message": {"id": "msg_f17_final", "type": "message", "role": "assistant",
             "content": [], "model": body.get("model", "claude"), "usage": _usage(input_tokens=900, output_tokens=0)}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
            {"type": "content_block_stop", "index": 0},
            # Real Anthropic message_delta.usage carries only output_tokens
            # (input_tokens never changes mid-stream so the wire never
            # repeats it here) -- NOT `_usage()`'s helper default shape,
            # which would otherwise clobber message_start's input_tokens
            # once `_step` does its finding-15 `usage.update(...)` merge.
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ])
        return
    _finish(h, [
        {"type": "message_start", "message": {"id": "msg_f17_think_tool", "type": "message", "role": "assistant",
         "content": [], "model": body.get("model", "claude"), "usage": _usage(input_tokens=321, output_tokens=0)}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "need to read the file first"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig_f17"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu_f17", "name": "Read", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"file_path"'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ':"/x-f17.py"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 18}},
        {"type": "message_stop"},
    ])


def _scn_ping(h, body):
    _start_sse(h)
    _sse_event(h, {"type": "message_start", "message": {"id": "msg_4", "type": "message", "role": "assistant",
                    "content": [], "model": body.get("model", "claude"), "usage": _usage(output_tokens=0)}})
    _sse_event(h, {"type": "ping"})
    _sse_event(h, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    _sse_event(h, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "pong"}})
    _sse_event(h, {"type": "content_block_stop", "index": 0})
    _sse_event(h, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": _usage()})
    _sse_event(h, {"type": "message_stop"})
    _end_sse(h)


def _scn_mid_stream_error(h, body):
    """A genuine Anthropic-shaped `error` EVENT arriving mid-stream (not an
    HTTP-level failure) -- connection stays open a while longer, matching
    stream.py's own "an error event is not necessarily end of stream" rule."""
    _start_sse(h)
    _sse_event(h, {"type": "message_start", "message": {"id": "msg_5", "type": "message", "role": "assistant",
                    "content": [], "model": body.get("model", "claude"), "usage": _usage(output_tokens=0)}})
    _sse_event(h, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    _sse_event(h, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial"}})
    _sse_event(h, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    _end_sse(h)


def _scn_rate_limit_429(h, body):
    _send_json(h, 429, {"type": "error", "error": {"type": "rate_limit_error", "message": "Number of request tokens has exceeded your rate limit"}},
               extra_headers={"retry-after": "3", "anthropic-ratelimit-requests-remaining": "0",
                               "anthropic-ratelimit-requests-reset": "2026-01-01T00:00:03Z"})


def _scn_overflow_400(h, body):
    """Anthropic's own overflow wording, verbatim shape (H5 scope E)."""
    _send_json(h, 400, {"type": "error", "error": {
        "type": "invalid_request_error", "message": "prompt is too long: 210000 tokens > 200000 maximum",
    }})


def _scn_count_tokens_42(h, body):
    """H8 must-do: a `/v1/messages/count_tokens`-shaped request (no
    `stream`/`max_tokens` fields -- the real endpoint's own contract)
    answered with Anthropic's real response shape, `{"input_tokens": N}`."""
    _send_json(h, 200, {"input_tokens": 42})


SCENARIOS = {
    "ok": _scn_ok,
    "thinking-and-signature": _scn_thinking_and_signature,
    "tool-use": _scn_tool_use,
    "thinking-then-tool-then-reply": _scn_thinking_then_tool_then_reply,
    "ping": _scn_ping,
    "mid-stream-error": _scn_mid_stream_error,
    "rate-limit-429": _scn_rate_limit_429,
    "overflow-400": _scn_overflow_400,
    "count-tokens-42": _scn_count_tokens_42,
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

        mock: MockAnthropic = self.server.mock  # type: ignore[attr-defined]
        with mock._lock:
            mock.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})

        # finding 16 test support: a caller can force a 404 for any request
        # whose path starts with one of these prefixes (query string and
        # all) -- used to exercise the ai-gateway -> serving-endpoints
        # Databricks fallback dance deterministically, regardless of what
        # scenario the model name would otherwise select.
        if any(self.path.startswith(p) for p in (mock.force_404_paths or ())):
            _send_json(self, 404, {"type": "error", "error": {"type": "not_found_error", "message": "not found"}})
            return

        # Scenario name travels as a suffix of the model name (matches
        # mock_databricks.py's own convention) -- e.g.
        # "claude-3-5-sonnet-ok" dispatches to "ok".
        model = body.get("model", "") or ""
        scenario = "ok"
        for name in sorted(SCENARIOS, key=len, reverse=True):
            if model.endswith(name):
                scenario = name
                break
        fn = SCENARIOS.get(scenario, _scn_ok)
        fn(self, body)

    def do_GET(self):
        _send_json(self, 404, {"error": "mock anthropic only serves POST"})


class MockAnthropic:
    def __init__(self):
        self._server = None
        self._thread = None
        self.requests: list = []
        self._lock = threading.Lock()
        # finding 16 test support -- see _Handler.do_POST's own comment.
        self.force_404_paths: set = set()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "MockAnthropic":
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

    def __enter__(self) -> "MockAnthropic":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

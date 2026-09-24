# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
test_bridge.py -- hermetic, black-box test suite for bridge.py (claude-bridge).

House style (see ../work-mcp/README.md "Tests" section): stdlib only, mock
servers run in-process, a single runner, nonzero exit on any real failure,
gate on the exit code -- never on printed text.

Everything here talks to bridge.py over HTTP/subprocess only. It never
imports bridge.py as a module, so it keeps working across refactors of the
bridge's internals.

Run:
    python test_bridge.py ; echo exit=$?
    uv run --script test_bridge.py ; echo exit=$?

If bridge.py does not exist yet (or fails to start), every test that needs
it is reported as SKIP rather than FAIL, and the summary says so plainly.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

# ============================================================================
# Paths / constants
# ============================================================================

REPO_DIR = Path(__file__).resolve().parent
BRIDGE_PY = REPO_DIR / "bridge.py"
BRIDGE_EXISTS = BRIDGE_PY.exists()


def free_port() -> int:
    """Bind to port 0 to get an OS-assigned free port, then release it.
    Small TOCTOU race is inherent to this pattern on every OS; acceptable
    for a test harness."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_is_free(port: int) -> bool:
    """Black-box reimplementation of bridge.py's own port_is_free(): true if
    nothing accepts a TCP connection on 127.0.0.1:port right now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


# ============================================================================
# Section 1: Mock OpenAI-dialect upstream
#
# ThreadingHTTPServer, HTTP/1.1, hand-rolled chunked SSE (so we control
# framing precisely). Scenario is selected purely from the "model" field of
# the parsed JSON body the bridge forwards -- e.g. "mock/split-args" ->
# scenario "split-args". This means the SAME shared server instance can back
# almost every test in this file; only the launcher tests need their own.
# ============================================================================


class ClientDisconnected(Exception):
    """Raised internally when a write to the client socket fails because the
    peer went away. Scenario code does not need to catch this -- the request
    handler's dispatcher does, once, and records it."""


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # A client that aborts mid-stream (by design, in several tests) makes
        # BaseHTTPRequestHandler's *next* keep-alive read raise a connection
        # error from inside socketserver's own plumbing, before our handler
        # code ever sees it. That is expected in this suite -- swallow it so
        # the console isn't full of scary but harmless tracebacks. Anything
        # else still prints, so real bugs are still visible.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError, OSError)):
            return
        traceback.print_exc()


def _write_safe(handler: "_Handler", data: bytes) -> None:
    try:
        handler.wfile.write(data)
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError) as e:
        raise ClientDisconnected(str(e)) from None


def send_json_response(handler: "_Handler", status: int, obj, extra_headers: dict | None = None) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "keep-alive")
    for k, v in (extra_headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    _write_safe(handler, body)


def start_sse(handler: "_Handler", status: int = 200, extra_headers: dict | None = None) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.send_header("Connection", "keep-alive")
    for k, v in (extra_headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()


def write_sse_chunk(handler: "_Handler", event_name, data) -> None:
    """`data` is a dict (JSON-encoded) or the literal string '[DONE]'."""
    parts = []
    if event_name is not None:
        parts.append(b"event: " + event_name.encode("utf-8") + b"\r\n")
    if data == "[DONE]":
        parts.append(b"data: [DONE]\r\n")
    else:
        parts.append(b"data: " + json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\r\n")
    parts.append(b"\r\n")
    frame = b"".join(parts)
    _write_safe(handler, b"%x\r\n" % len(frame) + frame + b"\r\n")


def end_sse(handler: "_Handler") -> None:
    try:
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
        pass


def abrupt_disconnect(handler: "_Handler") -> None:
    """Simulate a dead upstream: kill the TCP connection with no terminating
    chunk and no SSE error frame."""
    try:
        handler.connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        handler.connection.close()
    except OSError:
        pass
    handler.close_connection = True


def _finish(handler: "_Handler", chunks: list[dict], done: bool = True) -> None:
    start_sse(handler)
    for c in chunks:
        write_sse_chunk(handler, None, c)
    if done:
        write_sse_chunk(handler, None, "[DONE]")
    end_sse(handler)


# ---- scenarios -------------------------------------------------------------

def _scn_model(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": "pong"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


def _scn_small(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": "small-ok"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


def _scn_session_a(h, body):
    time.sleep(0.3)
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"content": "reply-from-session-a"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


def _scn_session_b(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"content": "reply-from-session-b"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


def _scn_split_args(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "Read", "arguments": ""}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"file_'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'path": "a'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])


def _scn_two_calls(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "Read", "arguments": '{"file_path": "a"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 1, "id": "call_b", "type": "function", "function": {"name": "Write", "arguments": '{"file_path": "b"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])


def _scn_interleaved(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "Read", "arguments": '{"file_'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 1, "id": "call_b", "type": "function", "function": {"name": "Write", "arguments": '{"file_'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'path":"a"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 1, "function": {"arguments": 'path":"b"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])


def _scn_repeated_id(h, body):
    # Two logically distinct calls (different index, different name/args)
    # that pathologically reuse the same upstream id. Bridge should key
    # buffering primarily by index and mint fresh toolu_ ids regardless.
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_dup", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"a"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 1, "id": "call_dup", "type": "function", "function": {"name": "Write", "arguments": '{"file_path":"b"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])


def _scn_missing_index(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"id": "call_only", "type": "function", "function": {"name": "Read", "arguments": '{"file_'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"function": {"arguments": 'path":"a"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])


def _scn_stop_with_tools(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_x", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"a"}'}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


def _scn_length_cut(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_x", "type": "function", "function": {"name": "Read", "arguments": '{"file_path": "some/very/lo'}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
    ])


def _scn_empty_args(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_x", "type": "function", "function": {"name": "ListDir", "arguments": ""}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])


def _scn_bad_json_args(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_x", "type": "function", "function": {"name": "Read", "arguments": '{"file_path": '}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])


def _scn_no_done(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": "no done marker"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ], done=False)


def _scn_no_done_no_finish(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": "abrupt end"}}]},
    ], done=False)


def _scn_usage_after_finish(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": "usage comes later"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 123, "completion_tokens": 7}},
    ])


def _scn_error_mid_stream(h, body):
    start_sse(h)
    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"content": "partial before boom"}}]})
    write_sse_chunk(h, None, {"error": {"message": "upstream exploded", "type": "server_error"}})
    end_sse(h)


def _scn_die_mid_stream(h, body):
    start_sse(h)
    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"content": "partial then dies"}}]})
    abrupt_disconnect(h)


def _scn_json_not_sse(h, body):
    send_json_response(h, 200, {
        "id": "chatcmpl-fake", "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "non-stream reply"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    })


def _scn_slow(h, body):
    start_sse(h)
    time.sleep(2.0)
    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"content": "finally"}}]})
    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    write_sse_chunk(h, None, "[DONE]")
    end_sse(h)


def _scn_long_abort(h, body):
    start_sse(h)
    for _ in range(60):
        write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"content": "x"}}]})
        time.sleep(0.1)
    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    write_sse_chunk(h, None, "[DONE]")
    end_sse(h)


def _scn_context_overflow_openai(h, body):
    # A ≤ L (max_tokens is the fixable cause): L=8192, A=2000 -> retry target
    # max_tokens = L - A - 256 = 5936. Once the bridge clamps to that or
    # below, this scenario succeeds instead of erroring again. (A small prompt
    # + a too-large requested completion is a realistic way this actually
    # happens; the retry budget is kept comfortably above finding 1's
    # min(requested, 4096) floor so this stays a genuinely fixable case.)
    max_tokens = (body or {}).get("max_tokens")
    if isinstance(max_tokens, (int, float)) and max_tokens <= 5936:
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "fit after retry"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])
        return
    send_json_response(h, 400, {"error": {
        "message": ("This model's maximum context length is 8192 tokens. However, you requested "
                    "9000 tokens (2000 in the messages, 7000 in the completion). Please reduce the "
                    "length of the messages or completion."),
        "type": "invalid_request_error",
    }})


def _scn_context_overflow_openrouter(h, body):
    # Real OpenRouter wording: "maximum context length is L tokens. However,
    # you requested about T tokens (A of text input, B in the output)." Here
    # A (68000) > L (65536) -- the PROMPT alone exceeds the limit, so this is
    # genuinely unfixable regardless of max_tokens -- bridge must rewrite to
    # "prompt is too long: T tokens > L maximum" (T forced > L), never retry.
    send_json_response(h, 400, {"error": {
        "message": ("This endpoint's maximum context length is 65536 tokens. However, you requested "
                    "about 71000 tokens (68000 of text input, 3000 in the output)."),
        "type": "invalid_request_error",
    }})


def _scn_context_overflow_openrouter_fixable(h, body):
    # Real OpenRouter wording (finding 4's own confirmed example): L=163840,
    # A=150000 (of text input) <= L -> fixable, retry budget = L-A-256=13584,
    # comfortably above finding 1's min(requested, 4096) floor.
    max_tokens = (body or {}).get("max_tokens")
    if isinstance(max_tokens, (int, float)) and max_tokens <= 13584:
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "openrouter retry succeeded"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])
        return
    send_json_response(h, 400, {"error": {
        "message": ("This endpoint's maximum context length is 163840 tokens. However, you requested "
                    "about 170000 tokens (150000 of text input, 20000 in the output)."),
        "type": "invalid_request_error",
    }})


def _scn_echo_max_tokens(h, body):
    mt = (body or {}).get("max_tokens")
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": json.dumps({"max_tokens": mt})}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


def _scn_bare_string_error_400(h, body):
    # finding 6: error is a bare string, not the usual {"message":...} dict.
    send_json_response(h, 400, {"error": "boom"})


def _scn_dict_metadata_raw_400(h, body):
    # finding 6: error.metadata.raw is a dict, not the expected string.
    send_json_response(h, 400, {"error": {
        "message": "maximum context length is 100 tokens.",
        "metadata": {"raw": {"nested": "dict-not-a-string"}},
    }})


def _scn_mid_stream_bare_string_error(h, body):
    # finding 6: a mid-stream {"error": "boom"} chunk, error as a bare string.
    start_sse(h)
    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"content": "partial "}}]})
    write_sse_chunk(h, None, {"error": "boom"})
    end_sse(h)


def _scn_null_text_part(h, body):
    # finding 6: a Databricks-style list content part with an explicit null "text".
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": [{"type": "text", "text": None}, {"type": "text", "text": "ok"}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


def _scn_tools_echo(h, body):
    payload = {
        "tools": [((t or {}).get("function") or {}).get("name") for t in (body.get("tools") or [])],
        "roles": [m.get("role") for m in (body.get("messages") or [])],
    }
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": json.dumps(payload)}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


SCENARIOS = {
    "model": _scn_model,
    "small": _scn_small,
    "session-a": _scn_session_a,
    "session-b": _scn_session_b,
    "split-args": _scn_split_args,
    "two-calls": _scn_two_calls,
    "interleaved": _scn_interleaved,
    "repeated-id": _scn_repeated_id,
    "missing-index": _scn_missing_index,
    "stop-with-tools": _scn_stop_with_tools,
    "length-cut": _scn_length_cut,
    "empty-args": _scn_empty_args,
    "bad-json-args": _scn_bad_json_args,
    "no-done": _scn_no_done,
    "no-done-no-finish": _scn_no_done_no_finish,
    "usage-after-finish": _scn_usage_after_finish,
    "error-mid-stream": _scn_error_mid_stream,
    "die-mid-stream": _scn_die_mid_stream,
    "json-not-sse": _scn_json_not_sse,
    "slow": _scn_slow,
    "long-abort": _scn_long_abort,
    "context-overflow-openai": _scn_context_overflow_openai,
    "context-overflow-openrouter": _scn_context_overflow_openrouter,
    "context-overflow-openrouter-fixable": _scn_context_overflow_openrouter_fixable,
    "tools-echo": _scn_tools_echo,
    "echo-max-tokens": _scn_echo_max_tokens,
    "bare-string-error-400": _scn_bare_string_error_400,
    "dict-metadata-raw-400": _scn_dict_metadata_raw_400,
    "mid-stream-bare-string-error": _scn_mid_stream_bare_string_error,
    "null-text-part": _scn_null_text_part,
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
            body = json.loads(raw) if raw else None
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            body = None

        mock: MockUpstream = self.server.mock  # type: ignore[attr-defined]
        with mock._lock:
            mock._requests.append({
                "method": "POST",
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
                "ts": time.monotonic(),
            })

        model = (body or {}).get("model", "") or ""
        scenario = model[len("mock/"):] if model.startswith("mock/") else "model"

        try:
            fn = SCENARIOS.get(scenario)
            if fn is not None:
                fn(self, body)
                return
            m = re.match(r"^error-(\d+)$", scenario)
            if m:
                code = int(m.group(1))
                extra = {"Retry-After": "2"} if code == 429 else {}
                send_json_response(self, code, {"error": {"message": f"mock upstream error {code}", "type": "mock_error", "code": code}}, extra)
            else:
                send_json_response(self, 404, {"error": f"unknown mock scenario '{scenario}'"})
        except ClientDisconnected:
            with mock._lock:
                mock.disconnect_events.append({"scenario": scenario, "ts": time.monotonic()})
        except Exception:
            traceback.print_exc()

    def do_GET(self):
        send_json_response(self, 404, {"error": "mock upstream only serves POST"})


class MockUpstream:
    def __init__(self):
        self._server: _ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._requests: list[dict] = []
        self._lock = threading.Lock()
        self.disconnect_events: list[dict] = []

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1"

    @property
    def requests(self) -> list[dict]:
        return self._requests

    def clear(self) -> None:
        with self._lock:
            self._requests.clear()
            self.disconnect_events.clear()

    def start(self) -> "MockUpstream":
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

    def __enter__(self) -> "MockUpstream":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# ---- Databricks/passthrough/--probe mock: path-dispatched, GET+POST -------
# (M4-M6) Unlike MockUpstream above (dispatches by "model" in the body),
# Databricks requests arrive on distinct PATHS with no model field at all on
# some routes, and --probe needs GET support too. Tests register a handler
# for an exact path right before issuing their request.


class _GenericMockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _dispatch(self, method: str):
        """Record the request, look up a per-path handler on self.server.mock, call it or 404."""
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except (ValueError, TypeError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw) if raw else None
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            body = None

        mock = self.server.mock  # type: ignore[attr-defined]
        with mock._lock:
            mock.requests.append({
                "method": method,
                "path": path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
                "ts": time.monotonic(),
            })

        fn = mock.handlers.get(path)
        if fn is not None:
            try:
                fn(self, body)
            except ClientDisconnected:
                with mock._lock:
                    mock.disconnect_events.append({"path": path, "ts": time.monotonic()})
            except Exception:
                traceback.print_exc()
            return
        send_json_response(self, 404, {"error": f"no mock handler registered for {method} {path}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


class GenericMockUpstream:
    """Path-dispatched HTTP mock (GET+POST): tests register a handler per exact path just before making their request, instead of a fixed named-scenario table."""

    def __init__(self):
        self._server: _ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.handlers: dict = {}
        self.requests: list = []
        self.disconnect_events: list = []
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        """Bound port (server must already be started)."""
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        """http://127.0.0.1:{port} -- NO path suffix (Databricks/OpenRouter paths are absolute)."""
        return f"http://127.0.0.1:{self.port}"

    def clear(self) -> None:
        """Clear recorded requests/disconnect_events (keeps registered handlers)."""
        with self._lock:
            self.requests.clear()
            self.disconnect_events.clear()

    def start(self) -> "GenericMockUpstream":
        """Bind an ephemeral port, start serving in a daemon thread, return self."""
        self._server = _ThreadingHTTPServer(("127.0.0.1", 0), _GenericMockHandler)
        self._server.mock = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Shut down and join the server thread; safe to call even if never started."""
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

    def __enter__(self) -> "GenericMockUpstream":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# ============================================================================
# Section 2: SSE parsing + Anthropic-stream validator
# ============================================================================


class _FrameAcc:
    """Accumulates SSE lines into complete (event_name, parsed_data) frames."""

    def __init__(self):
        self.event_name = None
        self.data_lines: list[str] = []

    def feed_line(self, line: str):
        """Returns 'DONE', a (event, data) tuple, or None (frame incomplete)."""
        if line == "":
            if not self.data_lines:
                self.event_name = None
                return None
            joined = "\n".join(self.data_lines)
            ev, self.event_name, self.data_lines = self.event_name, None, []
            if joined == "[DONE]":
                return "DONE"
            try:
                parsed = json.loads(joined)
            except (json.JSONDecodeError, ValueError):
                parsed = joined
            return (ev, parsed)
        if line.startswith(":"):
            return None
        if ":" in line:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "event":
            self.event_name = value
        elif field == "data":
            self.data_lines.append(value)
        return None


def read_sse_events(resp: http.client.HTTPResponse, deadline_s: float) -> list[dict]:
    """Read a chunked text/event-stream response incrementally. Stops on
    '[DONE]', on EOF, or once deadline_s has elapsed (raising TimeoutError
    with what was seen so far).

    Deliberately calls resp.readline() -- the HTTPResponse object's OWN
    dechunking reader -- and NOT resp.fp.readline() (the raw socket buffer).
    Reading .fp directly returns literal wire bytes: chunk-size hex lines and
    per-chunk trailing CRLFs interleaved with the actual SSE text. That
    "mostly" looks right only because a stream that ends with a `[DONE]`
    data line lets us stop before ever hitting the ambiguous bytes. A stream
    that ends on EOF with no `[DONE]` (explicitly part of this contract) needs
    the real dechunker: only resp.readline() reliably returns b'' exactly
    when the chunked BODY is complete, independent of whether the underlying
    TCP connection also closes.
    """
    start = time.monotonic()
    events: list[dict] = []
    acc = _FrameAcc()
    while True:
        if time.monotonic() - start > deadline_s:
            raise TimeoutError(f"SSE read timed out after {deadline_s}s; {len(events)} events so far: {events!r}")
        try:
            raw = resp.readline()
        except (socket.timeout, TimeoutError) as e:
            raise TimeoutError(f"SSE socket read timed out; {len(events)} events so far: {events!r}") from e
        if raw == b"":
            result = acc.feed_line("")
            if isinstance(result, tuple):
                events.append({"event": result[0], "data": result[1]})
            break
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        result = acc.feed_line(line)
        if result == "DONE":
            break
        if isinstance(result, tuple):
            events.append({"event": result[0], "data": result[1]})
    return events


def validate_anthropic_stream(events: list[dict]) -> None:
    """Applies to every WELL-FORMED streaming response (i.e. not a
    deliberately-truncated error/abort stream, which by contract never sends
    message_stop -- those get their own bespoke assertions instead)."""
    if not events:
        raise AssertionError("no SSE events received")
    first = events[0]
    if not (isinstance(first.get("data"), dict) and first["data"].get("type") == "message_start"):
        raise AssertionError(f"first event must be message_start, got: {first!r}")
    last = events[-1]
    if not (isinstance(last.get("data"), dict) and last["data"].get("type") == "message_stop"):
        raise AssertionError(f"last event must be message_stop, got: {last!r}")

    starts = [e for e in events if isinstance(e.get("data"), dict) and e["data"].get("type") == "message_start"]
    stops = [e for e in events if isinstance(e.get("data"), dict) and e["data"].get("type") == "message_stop"]
    if len(starts) != 1:
        raise AssertionError(f"expected exactly one message_start, found {len(starts)}")
    if len(stops) != 1:
        raise AssertionError(f"expected exactly one message_stop, found {len(stops)}")

    for idx, ev in enumerate(events):
        data = ev.get("data")
        if isinstance(data, dict) and "type" in data:
            if ev.get("event") != data["type"]:
                raise AssertionError(f"event index {idx}: SSE event name {ev.get('event')!r} != data.type {data['type']!r}: {ev!r}")

    ignored = {"ping", "message_start", "message_delta", "message_stop", "error"}
    open_idx: set[int] = set()
    seen_idx: set[int] = set()
    for idx, ev in enumerate(events):
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        t = data.get("type")
        if t in ignored:
            continue
        i = data.get("index")
        if not isinstance(i, int):
            raise AssertionError(f"event index {idx} type {t!r} missing/invalid 'index': {ev!r}")
        if t == "content_block_start":
            if i in open_idx:
                raise AssertionError(f"event {idx}: content_block_start reused open index {i}")
            open_idx.add(i)
            seen_idx.add(i)
        elif t == "content_block_delta":
            if i not in open_idx:
                raise AssertionError(f"event {idx}: content_block_delta on unopened index {i}")
        elif t == "content_block_stop":
            if i not in open_idx:
                raise AssertionError(f"event {idx}: content_block_stop on unopened index {i}")
            open_idx.discard(i)
    if open_idx:
        raise AssertionError(f"blocks opened but never stopped: {sorted(open_idx)}")
    sorted_seen = sorted(seen_idx)
    if sorted_seen != list(range(len(sorted_seen))):
        raise AssertionError(f"block indices not contiguous from 0: {sorted_seen}")

    found = False
    for ev in events:
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        usage = data.get("usage")
        if isinstance(usage, dict) and usage.get("output_tokens") is not None:
            ot = usage["output_tokens"]
            if not isinstance(ot, int) or isinstance(ot, bool) or ot < 0:
                raise AssertionError(f"output_tokens must be a non-negative int, got {ot!r}")
            found = True
            break
    if not found:
        raise AssertionError("no usage.output_tokens found in any event")


def assemble_message(events: list[dict]) -> dict:
    """Reconstruct a logical Anthropic Message from a raw event list, the way
    an SDK would, so individual tests can assert on content/stop_reason/usage
    instead of walking raw events by hand."""
    msg: dict = {"content": [], "stop_reason": None, "stop_sequence": None, "usage": {}, "model": None, "role": None, "id": None}
    blocks: dict[int, dict] = {}
    order: list[int] = []
    for ev in events:
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        t = data.get("type")
        if t == "message_start":
            m = data.get("message") or {}
            msg["id"] = m.get("id")
            msg["role"] = m.get("role")
            msg["model"] = m.get("model")
            if isinstance(m.get("usage"), dict):
                msg["usage"].update(m["usage"])
        elif t == "content_block_start":
            i = data["index"]
            block = dict(data.get("content_block") or {})
            if block.get("type") == "tool_use":
                block["_input_json_parts"] = []
            blocks[i] = block
            order.append(i)
        elif t == "content_block_delta":
            i = data["index"]
            block = blocks.setdefault(i, {})
            delta = data.get("delta") or {}
            dt = delta.get("type")
            if dt == "text_delta":
                block["type"] = block.get("type", "text")
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif dt == "thinking_delta":
                block["type"] = block.get("type", "thinking")
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif dt == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif dt == "input_json_delta":
                block.setdefault("_input_json_parts", []).append(delta.get("partial_json", ""))
        elif t == "content_block_stop":
            i = data["index"]
            block = blocks.get(i, {})
            if "_input_json_parts" in block:
                raw = "".join(block.pop("_input_json_parts"))
                try:
                    block["input"] = json.loads(raw) if raw.strip() else {}
                except (json.JSONDecodeError, ValueError):
                    block["input"] = {"_raw_unparsed": raw}
        elif t == "message_delta":
            d = data.get("delta") or {}
            if "stop_reason" in d:
                msg["stop_reason"] = d["stop_reason"]
            if "stop_sequence" in d:
                msg["stop_sequence"] = d["stop_sequence"]
            if isinstance(data.get("usage"), dict):
                msg["usage"].update(data["usage"])
    msg["content"] = [blocks[i] for i in order]
    return msg


def tool_use_blocks(msg: dict) -> list[dict]:
    return [b for b in msg["content"] if b.get("type") == "tool_use"]


def text_of(msg: dict) -> str:
    return "".join(b.get("text", "") for b in msg["content"] if b.get("type") == "text")


def extract_text(content) -> str:
    """Anthropic message `content` can be a plain string or a list of typed
    blocks; normalize either into concatenated text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


# ============================================================================
# Section 3: HTTP helpers + Anthropic request builders
# ============================================================================


def http_call(port: int, method: str, path: str, body=None, headers: dict | None = None,
              timeout: float = 10.0, conn: http.client.HTTPConnection | None = None):
    """Returns (status, resp_headers_lower, payload, conn). `payload` is a
    parsed SSE event list for text/event-stream responses, else parsed JSON
    (or raw text if not JSON). Caller owns closing `conn`."""
    if conn is None:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    ctype = resp.getheader("Content-Type", "") or ""
    status = resp.status
    resp_headers = {k.lower(): v for k, v in resp.getheaders()}
    if "text/event-stream" in ctype:
        payload = read_sse_events(resp, deadline_s=timeout)
    else:
        raw = resp.read()
        try:
            payload = json.loads(raw) if raw else None
        except (json.JSONDecodeError, ValueError):
            payload = raw.decode("utf-8", "replace")
    return status, resp_headers, payload, conn


def areq(model: str, messages: list, system=None, tools=None, tool_choice=None,
          max_tokens: int = 256, stream: bool = True, extra: dict | None = None) -> dict:
    body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": stream}
    if system is not None:
        body["system"] = system
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if extra:
        body.update(extra)
    return body


def tool_def(name: str, description: str = "a test tool", schema: dict | None = None, defer_loading=None) -> dict:
    d = {"name": name, "description": description, "input_schema": schema or {"type": "object", "properties": {}}}
    if defer_loading is not None:
        d["defer_loading"] = defer_loading
    return d


def tool_reference(name: str, key: str = "tool_name") -> dict:
    return {"type": "tool_reference", key: name}


# ============================================================================
# Section 4: bridge.py --serve fixture (subprocess)
# ============================================================================


class BridgeStartError(RuntimeError):
    pass


class BridgeProc:
    """Starts `bridge.py --serve` as a subprocess against a mock upstream,
    waits for /healthz, reads the per-launch token. One instance is shared
    across almost every test in this file (see module-level SHARED_CTX)."""

    def __init__(self, mock: MockUpstream, extra_env: dict | None = None, port: int | None = None):
        self.mock = mock
        self.port = port or free_port()
        self.tmp_root = Path(tempfile.mkdtemp(prefix="claude-bridge-test-"))
        self.state_dir = self.tmp_root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.home_dir = self.tmp_root / "home"
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.env_file = self.tmp_root / "env_file.txt"
        self.env_file.write_text("", encoding="utf-8")
        self.log_path = self.tmp_root / "serve-stdio.log"
        self.proc: subprocess.Popen | None = None
        self.token: str | None = None
        self.env = {
            "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
            "OPENROUTER_API_KEY": "test-key",
            "BRIDGE_ENV_FILE": str(self.env_file),
            "BRIDGE_TEST_HOME": str(self.home_dir),
            "BRIDGE_PING_INTERVAL": "0.2",
            "BRIDGE_MODEL": "or:mock/model",
            "BRIDGE_MODEL_SMALL": "or:mock/small",
        }
        if extra_env:
            self.env.update(extra_env)

    def start(self, timeout: float = 15.0) -> "BridgeProc":
        full_env = dict(os.environ)
        full_env.update(self.env)
        full_env.pop("CLAUDECODE", None)
        args = [sys.executable, str(BRIDGE_PY), "--serve", "--port", str(self.port), "--state-dir", str(self.state_dir)]
        log_fh = open(self.log_path, "wb")
        self.proc = subprocess.Popen(args, env=full_env, stdout=log_fh, stderr=subprocess.STDOUT, cwd=str(REPO_DIR))
        deadline = time.monotonic() + timeout
        last_err: Exception | str | None = None
        healthy = False
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                log_fh.close()
                out = self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""
                raise BridgeStartError(f"bridge --serve exited early with code {self.proc.returncode}: {out[-3000:]}")
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
                c.request("GET", "/healthz")
                r = c.getresponse()
                r.read()
                c.close()
                if r.status == 200:
                    healthy = True
                    break
                last_err = f"healthz status {r.status}"
            except (OSError, http.client.HTTPException) as e:
                last_err = e
            time.sleep(0.2)
        if not healthy:
            self.stop()
            raise BridgeStartError(f"bridge --serve never became healthy on port {self.port}: {last_err}")

        token_path = self.state_dir / "token"
        t0 = time.monotonic()
        while not token_path.exists() and time.monotonic() - t0 < 5:
            time.sleep(0.1)
        if not token_path.exists():
            self.stop()
            raise BridgeStartError(f"token file never appeared at {token_path}")
        self.token = token_path.read_text(encoding="utf-8").strip()
        return self

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if self.token and self.proc.poll() is None:
                c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                c.request("POST", "/shutdown", headers={"x-api-key": self.token})
                try:
                    c.getresponse().read()
                except Exception:
                    pass
                c.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
        self.proc = None
        shutil.rmtree(self.tmp_root, ignore_errors=True)


# ============================================================================
# Section 5: launcher fixture (bridge.py launch, fake claude)
# ============================================================================

_FAKE_CLAUDE_PY = r'''
import sys, os, json

argv = sys.argv[1:]

# Read the --settings file's OWN content now, from inside this process,
# rather than leaving it to the test to read after we exit: a real bridge
# may delete a launch-scoped settings file promptly once its child (us)
# exits, so reading it from the outside afterward would race a cleanup step
# that has nothing to do with the behavior under test.
settings_content = None
settings_path = None
for i, a in enumerate(argv):
    if a == "--settings" and i + 1 < len(argv):
        settings_path = argv[i + 1]
        break
    if isinstance(a, str) and a.startswith("--settings="):
        settings_path = a.split("=", 1)[1]
        break
if settings_path:
    try:
        with open(settings_path, "r", encoding="utf-8") as fh:
            settings_content = json.load(fh)
    except (OSError, ValueError) as e:
        settings_content = {"_read_error": f"{type(e).__name__}: {e}"}

dump_path = os.environ["FAKE_CLAUDE_DUMP"]
payload = {"argv": argv, "env": dict(os.environ), "settings_path": settings_path, "settings_content": settings_content}
with open(dump_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh)

hijack_path = os.environ.get("FAKE_CLAUDE_HIJACK_SETTINGS")
if hijack_path:
    try:
        with open(hijack_path, "w", encoding="utf-8") as fh:
            json.dump({"model": "hijacked-by-fake-claude"}, fh)
    except OSError:
        pass
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT_CODE", "7")))
'''


def make_fake_claude(tmp_root: Path) -> Path:
    """Writes a fake `claude` as a tiny .cmd shim around a Python script,
    since bridge.py Popen()s BRIDGE_CLAUDE_EXE directly (no shell) and a
    .cmd is the standard, well-established way to make that runnable on
    Windows without touching the real `claude` install."""
    py_path = tmp_root / "fake_claude.py"
    py_path.write_text(_FAKE_CLAUDE_PY, encoding="utf-8")
    cmd_path = tmp_root / "fake_claude.cmd"
    cmd_path.write_text(
        "@echo off\r\n"
        f'"{sys.executable}" "{py_path}" %*\r\n'
        "exit /b %ERRORLEVEL%\r\n",
        encoding="utf-8",
    )
    return cmd_path


class LauncherCtx:
    """Isolated fixture for `bridge.py launch` tests: its own tmp state/home,
    its own fake claude, its own mock upstream. Spawns a DETACHED background
    bridge server as a side effect (that's the feature under test), so
    callers MUST call stop() in a finally block."""

    def __init__(self):
        self.tmp_root = Path(tempfile.mkdtemp(prefix="claude-bridge-launch-test-"))
        self.state_dir = self.tmp_root / "state"
        self.home_dir = self.tmp_root / "home"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.home_dir / ".claude").mkdir(parents=True, exist_ok=True)
        self.env_file = self.tmp_root / "env_file.txt"
        self.env_file.write_text("", encoding="utf-8")
        self.settings_path = self.home_dir / ".claude" / "settings.json"
        self.settings_path.write_text(json.dumps({"model": "sonnet-preexisting"}), encoding="utf-8")
        self.fake_claude = make_fake_claude(self.tmp_root)
        self.dump_path = self.tmp_root / "dump.json"
        self.mock = MockUpstream().start()
        self.port = free_port()

    def run_launch(self, model: str = "or:mock/model", claude_args: list[str] | None = None,
                    exit_code: int = 7, hijack_settings: bool = False, timeout: float = 20.0,
                    extra_env: dict | None = None, raw_launch_args: list[str] | None = None) -> subprocess.CompletedProcess:
        """`raw_launch_args`, when given, is used VERBATIM as the argv right
        after the literal "launch" token (bypassing the model/claude_args/
        `--port` convenience building below entirely) -- needed by the
        finding-14 argv-parsing tests, which must control exactly where (or
        whether) a "--" appears."""
        claude_args = claude_args if claude_args is not None else ["-p", "hi"]
        if self.dump_path.exists():
            self.dump_path.unlink()
        env = dict(os.environ)
        env.update({
            "BRIDGE_OPENROUTER_BASE_URL": self.mock.base_url,
            "OPENROUTER_API_KEY": "test-key",
            "BRIDGE_ENV_FILE": str(self.env_file),
            "BRIDGE_TEST_HOME": str(self.home_dir),
            "BRIDGE_STATE_DIR": str(self.state_dir),
            "BRIDGE_CLAUDE_EXE": str(self.fake_claude),
            "FAKE_CLAUDE_DUMP": str(self.dump_path),
            "FAKE_CLAUDE_EXIT_CODE": str(exit_code),
        })
        env.pop("CLAUDECODE", None)
        if hijack_settings:
            env["FAKE_CLAUDE_HIJACK_SETTINGS"] = str(self.settings_path)
        else:
            env.pop("FAKE_CLAUDE_HIJACK_SETTINGS", None)
        if extra_env:
            env.update(extra_env)
        if raw_launch_args is not None:
            launch_argv = raw_launch_args
        else:
            launch_argv = ["--model", model, "--port", str(self.port), "--", *claude_args]
        args = [sys.executable, str(BRIDGE_PY), "launch", *launch_argv]
        return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)

    def read_dump(self) -> dict:
        return json.loads(self.dump_path.read_text(encoding="utf-8"))

    def server_json(self) -> dict | None:
        p = self.state_dir / "server.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def token(self) -> str | None:
        p = self.state_dir / "token"
        return p.read_text(encoding="utf-8").strip() if p.exists() else None

    def stop(self) -> None:
        try:
            tok = self.token()
            info = self.server_json()
            if tok and info and info.get("pid"):
                try:
                    c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                    c.request("POST", "/shutdown", headers={"x-api-key": tok})
                    try:
                        c.getresponse().read()
                    except Exception:
                        pass
                    c.close()
                except Exception:
                    pass
                time.sleep(0.3)
                pid = info.get("pid")
                try:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True, timeout=5)
                except Exception:
                    pass
        finally:
            try:
                self.mock.stop()
            except Exception:
                pass
            shutil.rmtree(self.tmp_root, ignore_errors=True)


# ============================================================================
# Section 6: test harness
# ============================================================================


class SkipTest(Exception):
    pass


class Ctx:
    def __init__(self):
        self.mock: MockUpstream | None = None
        self.bridge: BridgeProc | None = None
        self.bridge_start_error: str | None = None
        # Second shared fixture (M4-M6): a bridge.py --serve instance pointed
        # at a path-dispatched Databricks mock via BRIDGE_DBX_BASE_URL, kept
        # separate from the OpenRouter-only `bridge`/`mock` pair above so
        # neither fixture's config can leak into the other's tests.
        self.dbx_mock: "GenericMockUpstream | None" = None
        self.dbx_bridge: BridgeProc | None = None
        self.dbx_bridge_start_error: str | None = None
        self.checks = 0

    @property
    def token(self) -> str | None:
        return self.bridge.token if self.bridge else None

    def require_bridge(self) -> None:
        if not BRIDGE_EXISTS:
            raise SkipTest(f"bridge.py not found at {BRIDGE_PY}")
        if self.bridge is None:
            raise SkipTest(f"shared bridge fixture unavailable: {self.bridge_start_error}")

    def require_bridge_file_only(self) -> None:
        """For tests (e.g. the launcher group) that spin up their own
        bridge.py subprocess rather than using the shared fixture."""
        if not BRIDGE_EXISTS:
            raise SkipTest(f"bridge.py not found at {BRIDGE_PY}")

    def require_dbx_bridge(self) -> None:
        if not BRIDGE_EXISTS:
            raise SkipTest(f"bridge.py not found at {BRIDGE_PY}")
        if self.dbx_bridge is None:
            raise SkipTest(f"shared Databricks-mock bridge fixture unavailable: {self.dbx_bridge_start_error}")

    def dbx_call(self, path: str, body=None, method: str = "POST", headers: dict | None = None,
                 token: str | None = "__default__", timeout: float = 10.0,
                 conn: http.client.HTTPConnection | None = None):
        """Same contract as .call() but against the Databricks-mock-backed bridge instance."""
        hdrs = dict(headers or {})
        if token == "__default__":
            token = self.dbx_bridge.token if self.dbx_bridge else None
        lower_keys = {k.lower() for k in hdrs}
        if token is not None and "x-api-key" not in lower_keys and "authorization" not in lower_keys:
            hdrs["x-api-key"] = token
        assert self.dbx_bridge is not None
        return http_call(self.dbx_bridge.port, method, path, body=body, headers=hdrs, timeout=timeout, conn=conn)

    def check(self, description: str, condition: bool) -> None:
        self.checks += 1
        if not condition:
            raise AssertionError(description)

    def call(self, path: str, body=None, method: str = "POST", headers: dict | None = None,
              token: str | None = "__default__", timeout: float = 10.0,
              conn: http.client.HTTPConnection | None = None):
        hdrs = dict(headers or {})
        if token == "__default__":
            token = self.token
        lower_keys = {k.lower() for k in hdrs}
        if token is not None and "x-api-key" not in lower_keys and "authorization" not in lower_keys:
            hdrs["x-api-key"] = token
        assert self.bridge is not None
        return http_call(self.bridge.port, method, path, body=body, headers=hdrs, timeout=timeout, conn=conn)


TESTS: list[tuple[str, "Callable"]] = []


def test(fn):
    TESTS.append((fn.__name__, fn))
    return fn


def run_all(ctx: Ctx):
    results = []
    passed = failed = skipped = 0
    for name, fn in TESTS:
        t0 = time.monotonic()
        try:
            fn(ctx)
            results.append((name, "PASS", "", time.monotonic() - t0))
            passed += 1
        except SkipTest as e:
            results.append((name, "SKIP", str(e), time.monotonic() - t0))
            skipped += 1
        except AssertionError as e:
            results.append((name, "FAIL", str(e), time.monotonic() - t0))
            failed += 1
        except Exception as e:
            detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=6)}"
            results.append((name, "FAIL", detail, time.monotonic() - t0))
            failed += 1
    return results, passed, failed, skipped


# ---- Group A: HTTP / auth / basic endpoints -------------------------------


@test
def test_healthz_no_auth(ctx: Ctx):
    ctx.require_bridge()
    status, headers, payload, conn = http_call(ctx.bridge.port, "GET", "/healthz", timeout=5)
    conn.close()
    ctx.check(f"/healthz with no auth header returns 200, got {status}", status == 200)
    ctx.check(f"/healthz body is a JSON object, got {payload!r}", isinstance(payload, dict))


@test
def test_auth_missing_token_401_zero_upstream_calls(ctx: Ctx):
    ctx.require_bridge()
    before = len(ctx.mock.requests)
    body = areq("or:mock/model", [{"role": "user", "content": "hi"}])
    status, _, payload, conn = ctx.call("/v1/messages", body, token=None)
    conn.close()
    ctx.check(f"missing auth -> 401, got {status}: {payload!r}", status == 401)
    ctx.check("missing auth never reaches upstream", len(ctx.mock.requests) == before)


@test
def test_auth_wrong_token_401(ctx: Ctx):
    ctx.require_bridge()
    before = len(ctx.mock.requests)
    body = areq("or:mock/model", [{"role": "user", "content": "hi"}])
    status, _, payload, conn = ctx.call("/v1/messages", body, token="definitely-not-the-token")
    conn.close()
    ctx.check(f"wrong auth -> 401, got {status}: {payload!r}", status == 401)
    ctx.check("wrong auth never reaches upstream", len(ctx.mock.requests) == before)


@test
def test_auth_xapikey_accepted_basic_stream(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/model", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"x-api-key auth -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    ctx.check(f"basic stream text == 'pong', got {text_of(msg)!r}", text_of(msg) == "pong")


@test
def test_auth_bearer_header_accepted(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/model", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.call("/v1/messages", body, headers={"Authorization": f"Bearer {ctx.token}"})
    conn.close()
    ctx.check(f"Authorization: Bearer auth -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)


@test
def test_v1_models_empty(ctx: Ctx):
    ctx.require_bridge()
    status, _, payload, conn = ctx.call("/v1/models", method="GET")
    conn.close()
    ctx.check(f"/v1/models -> 200, got {status}", status == 200)
    ctx.check(f"/v1/models == {{'data': []}}, got {payload!r}", payload == {"data": []})


@test
def test_count_tokens_basic(ctx: Ctx):
    ctx.require_bridge()
    before = len(ctx.mock.requests)
    body = {"model": "or:mock/model", "messages": [{"role": "user", "content": "hello world, count me"}]}
    status, _, payload, conn = ctx.call("/v1/messages/count_tokens", body)
    conn.close()
    ctx.check(f"count_tokens -> 200, got {status}: {payload!r}", status == 200)
    ctx.check(f"count_tokens body has int input_tokens, got {payload!r}",
               isinstance(payload, dict) and isinstance(payload.get("input_tokens"), int) and not isinstance(payload.get("input_tokens"), bool))
    ctx.check(f"count_tokens input_tokens > 0, got {payload!r}", payload["input_tokens"] > 0)
    ctx.check("count_tokens makes zero upstream calls", len(ctx.mock.requests) == before)


@test
def test_count_tokens_beta_query_param(ctx: Ctx):
    ctx.require_bridge()
    body = {"model": "or:mock/model", "messages": [{"role": "user", "content": "hi"}]}
    status, _, payload, conn = ctx.call("/v1/messages/count_tokens?beta=true", body)
    conn.close()
    ctx.check(f"count_tokens?beta=true -> 200, got {status}: {payload!r}", status == 200)
    ctx.check("count_tokens?beta=true still returns input_tokens", isinstance(payload, dict) and "input_tokens" in payload)


@test
def test_messages_beta_query_param(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/model", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.call("/v1/messages?beta=true", body)
    conn.close()
    ctx.check(f"/v1/messages?beta=true -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)


@test
def test_keepalive_two_count_tokens_same_socket(ctx: Ctx):
    ctx.require_bridge()
    body1 = {"model": "or:mock/model", "messages": [{"role": "user", "content": "first"}]}
    status1, h1, payload1, conn = ctx.call("/v1/messages/count_tokens", body1)
    ctx.check(f"first count_tokens -> 200, got {status1}", status1 == 200)
    ctx.check("first response does not ask to close the connection", "close" not in (h1.get("connection", "")).lower())
    sock1 = conn.sock
    fd1 = sock1.fileno() if sock1 else None

    body2 = {"model": "or:mock/model", "messages": [{"role": "user", "content": "second"}]}
    status2, _, payload2, conn = ctx.call("/v1/messages/count_tokens", body2, conn=conn)
    ctx.check(f"second count_tokens on same conn -> 200, got {status2}", status2 == 200)
    ctx.check("http.client did not silently reconnect (same socket object)", conn.sock is sock1)
    if conn.sock is not None and fd1 is not None:
        ctx.check("same underlying fd across both calls", conn.sock.fileno() == fd1)
    conn.close()


@test
def test_401_then_valid_on_same_socket(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/model", [{"role": "user", "content": "hi"}])
    status1, _, payload1, conn = ctx.call("/v1/messages", body, token="bad-token-here")
    ctx.check(f"first (bad token) call -> 401, got {status1}", status1 == 401)
    status2, _, events, conn = ctx.call("/v1/messages", body, conn=conn)
    ctx.check(f"second (valid token) call on same socket -> 200, got {status2}", status2 == 200)
    validate_anthropic_stream(events)
    conn.close()


# ---- Group B: streaming shape edge cases -----------------------------------


@test
def test_no_done_marker_still_finalizes(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/no-done", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.call("/v1/messages", body, timeout=8)
    conn.close()
    ctx.check(f"no-[DONE] stream -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    ctx.check(f"text == 'no done marker', got {text_of(msg)!r}", text_of(msg) == "no done marker")
    ctx.check(f"stop_reason mapped to end_turn, got {msg['stop_reason']!r}", msg["stop_reason"] == "end_turn")


@test
def test_no_done_no_finish_still_finalizes(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/no-done-no-finish", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.call("/v1/messages", body, timeout=8)
    conn.close()
    ctx.check(f"neither [DONE] nor finish_reason -> still 200, got {status}", status == 200)
    validate_anthropic_stream(events)  # requires exactly one message_stop as the last event
    msg = assemble_message(events)
    ctx.check(f"text == 'abrupt end', got {text_of(msg)!r}", text_of(msg) == "abrupt end")
    ctx.check(f"stop_reason is a valid non-null value, got {msg['stop_reason']!r}",
               msg["stop_reason"] in ("end_turn", "max_tokens", "stop_sequence", "tool_use"))


@test
def test_usage_after_finish_chunk_exact(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/usage-after-finish", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"usage-after-finish -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    ctx.check(f"late usage chunk's output_tokens (7) is reflected exactly, got {msg['usage']!r}",
               msg["usage"].get("output_tokens") == 7)


@test
def test_json_not_sse_upstream_lies(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/json-not-sse", [{"role": "user", "content": "hi"}])
    status, headers, events, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"upstream plain-JSON reply still -> 200 SSE to the client, got {status}", status == 200)
    ctx.check("response is still translated into a real event list (not raw JSON)", isinstance(events, list) and len(events) > 0)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    ctx.check(f"text == 'non-stream reply', got {text_of(msg)!r}", text_of(msg) == "non-stream reply")


@test
def test_ping_during_silence(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/slow", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.call("/v1/messages", body, timeout=8)
    conn.close()
    ctx.check(f"slow upstream -> 200, got {status}", status == 200)
    pings = [e for e in events if isinstance(e.get("data"), dict) and e["data"].get("type") == "ping"]
    ctx.check(f"at least one ping event during the 2s silence (BRIDGE_PING_INTERVAL=0.2), got {len(pings)}", len(pings) >= 1)
    non_ping = [e for e in events if not (isinstance(e.get("data"), dict) and e["data"].get("type") == "ping")]
    validate_anthropic_stream(non_ping)
    msg = assemble_message(non_ping)
    ctx.check(f"text == 'finally', got {text_of(msg)!r}", text_of(msg) == "finally")


@test
def test_client_abort_closes_upstream_quickly(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/long-abort", [{"role": "user", "content": "hi"}])
    payload = json.dumps(body).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", ctx.bridge.port, timeout=10)
    conn.request("POST", "/v1/messages", body=payload, headers={"Content-Type": "application/json", "x-api-key": ctx.token})
    resp = conn.getresponse()
    ctx.check(f"long-abort stream starts -> 200, got {resp.status}", resp.status == 200)

    acc = _FrameAcc()
    got = 0
    deadline = time.monotonic() + 10
    while got < 2 and time.monotonic() < deadline:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        r = acc.feed_line(line)
        if isinstance(r, tuple):
            got += 1
    ctx.check("received at least 2 events before aborting", got >= 2)

    before = len(ctx.mock.disconnect_events)
    t_close = time.monotonic()
    conn.close()

    found = None
    poll_deadline = time.monotonic() + 3.0
    while time.monotonic() < poll_deadline:
        matches = [e for e in ctx.mock.disconnect_events[before:] if e["scenario"] == "long-abort"]
        if matches:
            found = matches[-1]
            break
        time.sleep(0.05)
    ctx.check("bridge propagated the client abort to the upstream connection", found is not None)
    if found is not None:
        dt = found["ts"] - t_close
        ctx.check(f"upstream connection closed within ~1.5s of client abort, took {dt:.2f}s", dt < 1.5)


# ---- Group C: tool-call delta buffering ------------------------------------


@test
def test_tool_call_args_split_across_chunks(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/split-args", [{"role": "user", "content": "read a file"}])
    status, _, events, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"split-args -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    calls = tool_use_blocks(msg)
    ctx.check(f"exactly one tool_use block, got {len(calls)}", len(calls) == 1)
    if calls:
        ctx.check(f"tool name == Read, got {calls[0].get('name')!r}", calls[0].get("name") == "Read")
        ctx.check(f"3-chunk-split arguments reassembled correctly, got {calls[0].get('input')!r}", calls[0].get("input") == {"file_path": "a"})
    ctx.check(f"stop_reason == tool_use, got {msg['stop_reason']!r}", msg["stop_reason"] == "tool_use")


@test
def test_two_tool_calls(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/two-calls", [{"role": "user", "content": "do two things"}])
    status, _, events, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"two-calls -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    calls = tool_use_blocks(msg)
    ctx.check(f"exactly two tool_use blocks, got {len(calls)}", len(calls) == 2)
    names = sorted(c.get("name") for c in calls)
    ctx.check(f"names are Read and Write, got {names!r}", names == ["Read", "Write"])
    by_name = {c.get("name"): c for c in calls}
    ctx.check(f"Read input == {{'file_path':'a'}}, got {by_name.get('Read', {}).get('input')!r}", by_name.get("Read", {}).get("input") == {"file_path": "a"})
    ctx.check(f"Write input == {{'file_path':'b'}}, got {by_name.get('Write', {}).get('input')!r}", by_name.get("Write", {}).get("input") == {"file_path": "b"})


@test
def test_interleaved_tool_call_indices(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/interleaved", [{"role": "user", "content": "do two things"}])
    status, _, events, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"interleaved -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    calls = tool_use_blocks(msg)
    ctx.check(f"exactly two tool_use blocks despite interleaved deltas, got {len(calls)}", len(calls) == 2)
    by_name = {c.get("name"): c for c in calls}
    ctx.check(f"Read input == {{'file_path':'a'}}, got {by_name.get('Read', {}).get('input')!r}", by_name.get("Read", {}).get("input") == {"file_path": "a"})
    ctx.check(f"Write input == {{'file_path':'b'}}, got {by_name.get('Write', {}).get('input')!r}", by_name.get("Write", {}).get("input") == {"file_path": "b"})


@test
def test_repeated_id_treated_as_distinct_calls(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/repeated-id", [{"role": "user", "content": "do two things"}])
    status, _, events, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"repeated-id -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    calls = tool_use_blocks(msg)
    ctx.check(f"two distinct tool_use blocks despite a duplicate upstream id (keyed by index), got {len(calls)}", len(calls) == 2)
    if len(calls) == 2:
        ctx.check(f"bridge mints distinct ids rather than reusing the upstream's duplicate, got {[c.get('id') for c in calls]!r}",
                   calls[0].get("id") != calls[1].get("id"))
        names = sorted(c.get("name") for c in calls)
        ctx.check(f"names are Read and Write, got {names!r}", names == ["Read", "Write"])


@test
def test_missing_index_single_tool_call(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/missing-index", [{"role": "user", "content": "read a file"}])
    status, _, events, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"missing-index -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    calls = tool_use_blocks(msg)
    ctx.check(f"exactly one tool_use block when upstream never sends an index, got {len(calls)}", len(calls) == 1)
    if calls:
        ctx.check(f"input reassembled correctly, got {calls[0].get('input')!r}", calls[0].get("input") == {"file_path": "a"})


@test
def test_stop_reason_forced_tool_use_even_if_provider_says_stop(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/stop-with-tools", [{"role": "user", "content": "read a file"}])
    status, _, events, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"stop-with-tools -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    ctx.check(f"stop_reason forced to tool_use even though upstream said 'stop', got {msg['stop_reason']!r}", msg["stop_reason"] == "tool_use")
    ctx.check("a tool_use block is present", len(tool_use_blocks(msg)) == 1)


@test
def test_length_cuts_off_tool_call(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/length-cut", [{"role": "user", "content": "read a file"}])
    status, _, events, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"length-cut -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    ctx.check(f"the unfinished tool call is dropped rather than emitted broken, got {tool_use_blocks(msg)!r}", len(tool_use_blocks(msg)) == 0)
    ctx.check(f"stop_reason == max_tokens, got {msg['stop_reason']!r}", msg["stop_reason"] == "max_tokens")


@test
def test_empty_arguments_becomes_empty_object(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/empty-args", [{"role": "user", "content": "list dir"}])
    status, _, events, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"empty-args -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    calls = tool_use_blocks(msg)
    ctx.check(f"one tool_use block, got {len(calls)}", len(calls) == 1)
    if calls:
        ctx.check(f"empty arguments string becomes {{}}, got {calls[0].get('input')!r}", calls[0].get("input") == {})


@test
def test_invalid_json_arguments_becomes_empty_object(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/bad-json-args", [{"role": "user", "content": "read a file"}])
    status, _, events, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"bad-json-args -> 200 (bridge must not crash on malformed JSON args), got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    calls = tool_use_blocks(msg)
    ctx.check(f"one tool_use block, got {len(calls)}", len(calls) == 1)
    if calls:
        ctx.check(f"invalid JSON arguments become {{}}, got {calls[0].get('input')!r}", calls[0].get("input") == {})


# ---- Group D: error / status mapping ---------------------------------------


@test
def test_error_mapping_table(ctx: Ctx):
    ctx.require_bridge()
    table = [
        (401, "authentication_error", 401, False),
        (403, "permission_error", 403, False),
        (404, "not_found_error", 404, False),
        (429, "rate_limit_error", 429, True),
        (500, "api_error", 500, True),
        (503, "overloaded_error", 529, True),
    ]
    for upstream_code, expected_type, expected_status, expect_retry in table:
        body = areq(f"or:mock/error-{upstream_code}", [{"role": "user", "content": "hi"}])
        status, headers, payload, conn = ctx.call("/v1/messages", body)
        conn.close()
        ctx.check(f"upstream {upstream_code} -> client status {expected_status}, got {status}", status == expected_status)
        ctx.check(f"upstream {upstream_code} -> arrives as plain JSON, not SSE (before any 200)",
                   "text/event-stream" not in (headers.get("content-type") or ""))
        ctx.check(f"upstream {upstream_code} -> response body is a JSON object, got {payload!r}", isinstance(payload, dict))
        err = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
        ctx.check(f"upstream {upstream_code} -> error.type == {expected_type!r}, got {err.get('type')!r}", err.get("type") == expected_type)
        retry_hdr = headers.get("x-should-retry")
        ctx.check(f"upstream {upstream_code} -> x-should-retry header present, got headers={headers!r}", retry_hdr is not None)
        if retry_hdr is not None:
            ctx.check(f"upstream {upstream_code} -> x-should-retry == {str(expect_retry).lower()!r}, got {retry_hdr!r}",
                       retry_hdr.strip().lower() == str(expect_retry).lower())
        if upstream_code == 429:
            ctx.check(f"429 -> a Retry-After hint surfaces somewhere in the response headers, got {headers!r}",
                       any("retry-after" in k for k in headers))


@test
def test_mid_stream_error_event_no_message_stop(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/error-mid-stream", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"mid-stream error still starts as 200 (already streaming when it happens), got {status}", status == 200)
    ctx.check("message_start is still the first event", bool(events) and isinstance(events[0].get("data"), dict) and events[0]["data"].get("type") == "message_start")
    error_events = [e for e in events if isinstance(e.get("data"), dict) and e["data"].get("type") == "error"]
    types_seen = [e["data"].get("type") for e in events if isinstance(e.get("data"), dict)]
    ctx.check(f"an error event is emitted mid-stream, event types seen: {types_seen!r}", len(error_events) >= 1)
    if error_events:
        ctx.check(f"error event's SSE 'event:' name matches its data.type, got {error_events[0].get('event')!r}", error_events[0].get("event") == "error")
        err_obj = error_events[0]["data"].get("error")
        ctx.check(f"error payload carries a non-empty message, got {error_events[0]['data']!r}",
                   isinstance(err_obj, dict) and bool(err_obj.get("message")))
    stop_events = [e for e in events if isinstance(e.get("data"), dict) and e["data"].get("type") == "message_stop"]
    ctx.check(f"no message_stop after a mid-stream error (contract: error then close, never both), got {len(stop_events)}", len(stop_events) == 0)


@test
def test_upstream_dies_mid_stream_becomes_error_event(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/die-mid-stream", [{"role": "user", "content": "hi"}], extra={"max_tokens": 256})
    status, _, events, conn = ctx.call("/v1/messages", body, timeout=15)
    conn.close()
    ctx.check(f"upstream TCP death still starts as 200, got {status}", status == 200)
    error_events = [e for e in events if isinstance(e.get("data"), dict) and e["data"].get("type") == "error"]
    types_seen = [e["data"].get("type") for e in events if isinstance(e.get("data"), dict)]
    ctx.check(f"a dead upstream socket is translated into a client-visible error event, types seen: {types_seen!r}", len(error_events) >= 1)
    stop_events = [e for e in events if isinstance(e.get("data"), dict) and e["data"].get("type") == "message_stop"]
    ctx.check(f"no message_stop after an upstream death, got {len(stop_events)}", len(stop_events) == 0)


@test
def test_bare_string_error_body_does_not_crash(ctx: Ctx):
    """finding 6: a bare {"error": "boom"} 400 body (error is a plain
    string, not the usual {"message":...} dict) used to crash
    map_upstream_error / the overflow-parsing block with AttributeError
    BEFORE any response was sent, dropping the socket with no status at all
    (the SDK then just sees a hang and retries with backoff)."""
    ctx.require_bridge()
    body = areq("or:mock/bare-string-error-400", [{"role": "user", "content": "hi"}])
    status, _, payload, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"malformed error body still yields a real 4xx, not a dropped connection, got {status}",
               400 <= status < 500)
    ctx.check(f"error body is a JSON object naming 'boom' somewhere, got {payload!r}",
               isinstance(payload, dict) and "boom" in json.dumps(payload))


@test
def test_dict_metadata_raw_does_not_crash(ctx: Ctx):
    """finding 6: error.metadata.raw as a dict (not the expected string)
    used to raise TypeError when concatenated as " " + raw_meta inside
    parse_context_overflow."""
    ctx.require_bridge()
    body = areq("or:mock/dict-metadata-raw-400", [{"role": "user", "content": "hi"}])
    status, _, payload, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"malformed metadata.raw still yields a real 4xx, not a dropped connection, got {status}",
               400 <= status < 500)
    ctx.check(f"error body is a JSON object, got {payload!r}", isinstance(payload, dict))


@test
def test_mid_stream_bare_string_error_chunk_does_not_crash(ctx: Ctx):
    """finding 6: a mid-stream {"error": "boom"} chunk (bare string, not a
    dict) used to raise AttributeError inside the stream consumer loop,
    which only caught connection errors -- the stream then died with
    neither an error event nor message_stop, just the bare chunked
    terminator from the `finally`."""
    ctx.require_bridge()
    body = areq("or:mock/mid-stream-bare-string-error", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.call("/v1/messages", body, timeout=10)
    conn.close()
    ctx.check(f"still starts as 200 (already streaming), got {status}", status == 200)
    error_events = [e for e in events if isinstance(e.get("data"), dict) and e["data"].get("type") == "error"]
    types_seen = [e["data"].get("type") for e in events if isinstance(e.get("data"), dict)]
    ctx.check(f"a clean error event is emitted instead of a silent crash, event types seen: {types_seen!r}",
               len(error_events) >= 1)


@test
def test_null_text_part_does_not_crash_stream(ctx: Ctx):
    """finding 6: a Databricks-style content part {"type":"text","text":null}
    used to raise TypeError inside flatten_content_parts's "".join(texts) --
    part.get("text", "") only substitutes the default when the KEY is
    missing, not when it's present but null."""
    ctx.require_bridge()
    body = areq("or:mock/null-text-part", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.call("/v1/messages", body, timeout=10)
    conn.close()
    ctx.check(f"still 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    ctx.check(f"the non-null text part still comes through, got {text_of(msg)!r}", "ok" in text_of(msg))


@test
def test_context_overflow_openai_retries_and_succeeds(ctx: Ctx):
    ctx.require_bridge()
    before = len(ctx.mock.requests)
    body = areq("or:mock/context-overflow-openai", [{"role": "user", "content": "a very long prompt " * 50}], max_tokens=7000)
    status, _, events, conn = ctx.call("/v1/messages", body, timeout=15)
    conn.close()
    ctx.check(f"bridge retries transparently -> client only ever sees success (200), got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    ctx.check(f"final text shows the retry succeeded, got {text_of(msg)!r}", text_of(msg) == "fit after retry")
    calls = ctx.mock.requests[before:]
    ctx.check(f"at least two upstream calls were made (original + retry), got {len(calls)}", len(calls) >= 2)
    last_max_tokens = (calls[-1]["body"] or {}).get("max_tokens")
    ctx.check(f"the retry clamps max_tokens to L-A-256=5936 or below (L=8192, A=2000), got {last_max_tokens!r}",
               isinstance(last_max_tokens, (int, float)) and last_max_tokens <= 5936)


@test
def test_context_overflow_openrouter_rewritten_prompt_too_long(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/context-overflow-openrouter", [{"role": "user", "content": "hi"}])
    status, _, payload, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"unfixable overflow (T > L) -> a real error status, not 200, got {status}", status >= 400)
    ctx.check(f"error body is JSON (fails before any SSE), got {payload!r}", isinstance(payload, dict))
    msg_text = ((payload or {}).get("error") or {}).get("message", "") if isinstance(payload, dict) else ""
    m = re.search(r"prompt is too long[^0-9]*(\d+)\s*tokens?\s*>\s*(\d+)", msg_text)
    ctx.check(f"error message matches Claude Code's own compaction-trigger regex, got {msg_text!r}", m is not None)
    if m:
        t_val, l_val = int(m.group(1)), int(m.group(2))
        ctx.check(f"T > L in the rewritten message (T={t_val}, L={l_val})", t_val > l_val)


@test
def test_context_overflow_openrouter_fixable_retries_and_succeeds(ctx: Ctx):
    """finding 4: OpenRouter's real wording states A as "(N of text input",
    never "(N in the messages" -- the bridge used to only ever look for the
    latter, so A was never known and every OpenRouter overflow was rewritten
    to prompt-too-long even when perfectly fixable by shrinking max_tokens."""
    ctx.require_bridge()
    before = len(ctx.mock.requests)
    body = areq("or:mock/context-overflow-openrouter-fixable", [{"role": "user", "content": "hi"}], max_tokens=20000)
    status, _, events, conn = ctx.call("/v1/messages", body, timeout=15)
    conn.close()
    ctx.check(f"fixable OpenRouter overflow -> client only ever sees success (200), got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    ctx.check(f"final text shows the retry succeeded, got {text_of(msg)!r}", text_of(msg) == "openrouter retry succeeded")
    calls = ctx.mock.requests[before:]
    ctx.check(f"at least two upstream calls were made (original + retry), got {len(calls)}", len(calls) >= 2)
    last_max_tokens = (calls[-1]["body"] or {}).get("max_tokens")
    ctx.check(f"the retry clamps max_tokens to L-A-256=13584 or below (L=163840, A=150000), got {last_max_tokens!r}",
               isinstance(last_max_tokens, (int, float)) and last_max_tokens <= 13584)


@test
def test_large_image_in_history_keeps_max_tokens_sane(ctx: Ctx):
    """finding 1: a large embedded image must not be counted at its raw
    base64 byte length / 4 when estimating tokens for the up-front
    max_tokens clamp -- that used to floor max_tokens at 1 (a ~450KB
    screenshot in history -> 1-token replies every later turn)."""
    ctx.require_bridge()
    big_b64 = "A" * 450_000  # simulate a large embedded screenshot
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "what is in this screenshot?"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": big_b64}},
        ]},
    ]
    body = areq("or:mock/echo-max-tokens", messages, max_tokens=4000)
    status, _, events, conn = ctx.call("/v1/messages", body, timeout=15)
    conn.close()
    ctx.check(f"large image in request -> still 200, got {status}", status == 200)
    msg = assemble_message(events)
    echoed = json.loads(text_of(msg))
    ctx.check(f"max_tokens sent upstream is NOT floored to 1 despite the large image, got {echoed!r}",
               isinstance(echoed.get("max_tokens"), int) and echoed["max_tokens"] > 100)
    ctx.check(f"max_tokens stays at what was requested (4000) since a flat-per-image estimate leaves "
               f"plenty of headroom, got {echoed!r}", echoed.get("max_tokens") == 4000)


# ---- Group E: request translation (Anthropic -> OpenAI) -------------------


@test
def test_canonical_request_translation_example(ctx: Ctx):
    """The exact before/after example from the design review, section 3."""
    ctx.require_bridge()
    system = [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "a"}},
            {"type": "tool_use", "id": "toolu_2", "name": "Screenshot", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True, "content": "ENOENT"},
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
            ]},
            {"type": "text", "text": "<system-reminder>after tool use</system-reminder>"},
        ]},
    ]
    body = areq("or:mock/model", messages, system=system)
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"canonical translation request -> 200, got {status}", status == 200)
    up_messages = ctx.mock.requests[-1]["body"]["messages"]

    sys_msgs = [m for m in up_messages if m["role"] == "system"]
    ctx.check(f"exactly one system message, got {sys_msgs!r}", len(sys_msgs) == 1)
    ctx.check(f"system content flattened to the plain string 'SYS' (cache_control dropped), got {sys_msgs[0]['content']!r}",
               sys_msgs[0]["content"] == "SYS")

    asst = [m for m in up_messages if m["role"] == "assistant"]
    ctx.check(f"exactly one assistant message, got {len(asst)}", len(asst) == 1)
    a = asst[0] if asst else {}
    ctx.check(f"assistant content is null (tool-only turn), got {a.get('content')!r}", a.get("content") is None)
    calls = a.get("tool_calls") or []
    ctx.check(f"two tool_calls in order, got {len(calls)}", len(calls) == 2)
    if len(calls) == 2:
        ctx.check(f"tool_calls[0].id == toolu_1, got {calls[0].get('id')!r}", calls[0].get("id") == "toolu_1")
        ctx.check(f"tool_calls[0].function.name == Read, got {calls[0].get('function', {}).get('name')!r}", calls[0].get("function", {}).get("name") == "Read")
        args0 = json.loads(calls[0]["function"]["arguments"])
        ctx.check(f"tool_calls[0] arguments parse to {{'file_path':'a'}}, got {args0!r}", args0 == {"file_path": "a"})
        ctx.check(f"tool_calls[1].id == toolu_2, got {calls[1].get('id')!r}", calls[1].get("id") == "toolu_2")

    tool_msgs = [m for m in up_messages if m["role"] == "tool"]
    ctx.check(f"two tool-role messages, got {len(tool_msgs)}", len(tool_msgs) == 2)
    t1 = next((m for m in tool_msgs if m.get("tool_call_id") == "toolu_1"), None)
    ctx.check("a tool message for toolu_1 exists", t1 is not None)
    if t1 is not None:
        ctx.check(f"toolu_1 content == '[tool error] ENOENT' (is_error flag surfaced), got {t1['content']!r}", t1["content"] == "[tool error] ENOENT")
    t2 = next((m for m in tool_msgs if m.get("tool_call_id") == "toolu_2"), None)
    ctx.check("a tool message for toolu_2 exists", t2 is not None)
    if t2 is not None:
        ctx.check(f"toolu_2 content == '(image in next message)' (image hoisted out), got {t2['content']!r}", t2["content"] == "(image in next message)")

    user_msgs = [m for m in up_messages if m["role"] == "user"]
    ctx.check(f"at least one user message remains, got {len(user_msgs)}", len(user_msgs) >= 1)
    img_msg = None
    for m in user_msgs:
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
            img_msg = m
            break
    ctx.check("a user message carries the hoisted image_url part", img_msg is not None)
    if img_msg is not None:
        parts = img_msg["content"]
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
        ctx.check(f"hoisted-image user message labels it 'Image from toolu_2:', got {texts!r}", any("Image from toolu_2" in t for t in texts))
        ctx.check(f"the original reminder text also survives, got {texts!r}", any("system-reminder" in t for t in texts))
        img_parts = [p for p in parts if isinstance(p, dict) and p.get("type") == "image_url"]
        ctx.check("hoisted image_url exists", len(img_parts) == 1)
        if img_parts:
            ctx.check(f"image_url is a data: URI with the right media type, got {img_parts[0]!r}",
                       img_parts[0].get("image_url", {}).get("url", "").startswith("data:image/png;base64,"))


@test
def test_billing_header_system_block_dropped(ctx: Ctx):
    ctx.require_bridge()
    system = [
        {"type": "text", "text": "x-anthropic-billing-header: acct-123"},
        {"type": "text", "text": "You are a helpful assistant."},
    ]
    body = areq("or:mock/model", [{"role": "user", "content": "hi"}], system=system)
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"billing-header request -> 200, got {status}", status == 200)
    up = ctx.mock.requests[-1]["body"]
    sys_msgs = [m for m in up["messages"] if m["role"] == "system"]
    ctx.check("exactly one system message", len(sys_msgs) == 1)
    sys_text = extract_text(sys_msgs[0]["content"]) if sys_msgs else ""
    ctx.check(f"billing-header block dropped, got {sys_text!r}", "x-anthropic-billing-header" not in sys_text)
    ctx.check(f"real system text preserved, got {sys_text!r}", "helpful assistant" in sys_text)


@test
def test_tool_use_with_no_result_gets_placeholder(ctx: Ctx):
    ctx.require_bridge()
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_a", "name": "Read", "input": {"file_path": "a"}},
            {"type": "tool_use", "id": "toolu_b", "name": "Read", "input": {"file_path": "b"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_b", "content": "contents of b"},
        ]},
    ]
    body = areq("or:mock/model", messages)
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"missing-result request -> 200, got {status}", status == 200)
    up = ctx.mock.requests[-1]["body"]
    tool_msgs = [m for m in up["messages"] if m["role"] == "tool"]
    ctx.check(f"two tool messages emitted (placeholder + real), got {len(tool_msgs)}", len(tool_msgs) == 2)
    by_id = {m.get("tool_call_id"): m for m in tool_msgs}
    ctx.check(f"toolu_a (no result in history) gets '(no result)', got {by_id.get('toolu_a', {}).get('content')!r}",
               by_id.get("toolu_a", {}).get("content") == "(no result)")
    ctx.check(f"toolu_b keeps its real content, got {by_id.get('toolu_b', {}).get('content')!r}",
               by_id.get("toolu_b", {}).get("content") == "contents of b")
    ids_in_order = [m.get("tool_call_id") for m in tool_msgs]
    ctx.check(f"tool messages emitted in tool_use order (a before b), got {ids_in_order!r}", ids_in_order == ["toolu_a", "toolu_b"])


@test
def test_unknown_tool_result_id_becomes_user_text(ctx: Ctx):
    ctx.require_bridge()
    messages = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_never_existed", "content": "orphaned result"},
        ]},
    ]
    body = areq("or:mock/model", messages)
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"orphaned tool_result -> 200 (bridge must not crash/reject), got {status}", status == 200)
    up = ctx.mock.requests[-1]["body"]
    tool_msgs = [m for m in up["messages"] if m["role"] == "tool"]
    ctx.check(f"no tool-role message is fabricated for an unknown id, got {tool_msgs!r}", len(tool_msgs) == 0)
    user_msgs = [m for m in up["messages"] if m["role"] == "user"]
    ctx.check(f"a user message carries the orphaned content as plain text, got {user_msgs!r}",
               any("orphaned result" in extract_text(m.get("content")) for m in user_msgs))


@test
def test_whitespace_only_text_dropped(ctx: Ctx):
    ctx.require_bridge()
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "   \n\t  "},
            {"type": "text", "text": "real text"},
        ]},
    ]
    body = areq("or:mock/model", messages)
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"whitespace-only-plus-real-text -> 200, got {status}", status == 200)
    up = ctx.mock.requests[-1]["body"]
    user_msgs = [m for m in up["messages"] if m["role"] == "user"]
    ctx.check("exactly one user message", len(user_msgs) == 1)
    combined = extract_text(user_msgs[0]["content"]) if user_msgs else ""
    ctx.check(f"whitespace-only block dropped, real text kept, got {combined!r}", combined.strip() == "real text")


@test
def test_whitespace_only_message_dropped_and_neighbors_merge(ctx: Ctx):
    ctx.require_bridge()
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "first"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": [{"type": "text", "text": "   "}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    body = areq("or:mock/model", messages)
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"whitespace-only message in the middle -> 200, got {status}", status == 200)
    up = ctx.mock.requests[-1]["body"]
    non_system = [m for m in up["messages"] if m["role"] != "system"]
    ctx.check(f"whitespace-only message dropped and its now-adjacent same-role neighbors merged, got {non_system!r}", len(non_system) == 2)
    if len(non_system) == 2:
        ctx.check(f"first is user 'first', got {non_system[0]!r}",
                   non_system[0]["role"] == "user" and "first" in extract_text(non_system[0]["content"]))
        ctx.check(f"second is assistant with both ok and done merged, got {non_system[1]!r}",
                   non_system[1]["role"] == "assistant" and "ok" in extract_text(non_system[1]["content"]) and "done" in extract_text(non_system[1]["content"]))


@test
def test_consecutive_same_role_messages_merged(ctx: Ctx):
    ctx.require_bridge()
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "part one"}]},
        {"role": "user", "content": [{"type": "text", "text": "part two"}]},
    ]
    body = areq("or:mock/model", messages)
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"two consecutive user messages -> 200, got {status}", status == 200)
    up = ctx.mock.requests[-1]["body"]
    user_msgs = [m for m in up["messages"] if m["role"] == "user"]
    ctx.check(f"consecutive same-role messages merged into one, got {len(user_msgs)}", len(user_msgs) == 1)
    if user_msgs:
        combined = extract_text(user_msgs[0]["content"])
        ctx.check(f"merged content has both parts, got {combined!r}", "part one" in combined and "part two" in combined)


@test
def test_lone_surrogate_in_tool_output_does_not_crash(ctx: Ctx):
    ctx.require_bridge()
    weird = "before\ud83dafter"  # lone high surrogate, no low surrogate pair
    messages = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_x", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": weird}]},
    ]
    body = areq("or:mock/model", messages)
    # ensure_ascii=True (the default) escapes the lone surrogate as \udXXX text,
    # which is plain ASCII on the wire -- exactly how a real client would send it.
    payload_bytes = json.dumps(body, ensure_ascii=True).encode("ascii")
    before = len(ctx.mock.requests)
    conn = http.client.HTTPConnection("127.0.0.1", ctx.bridge.port, timeout=10)
    conn.request("POST", "/v1/messages", body=payload_bytes, headers={"Content-Type": "application/json", "x-api-key": ctx.token})
    resp = conn.getresponse()
    ctx.check(f"lone surrogate in tool output -> still 200, no crash, got {resp.status}", resp.status == 200)
    ctype = resp.getheader("Content-Type", "") or ""
    if "text/event-stream" in ctype:
        events = read_sse_events(resp, deadline_s=10)
        validate_anthropic_stream(events)
    else:
        resp.read()
    conn.close()
    ctx.check("bridge still forwarded the request to upstream despite the lone surrogate", len(ctx.mock.requests) > before)


@test
def test_tool_choice_mapping(ctx: Ctx):
    ctx.require_bridge()
    tools = [tool_def("Read")]
    table = [("auto", "auto"), ("any", "required"), ("none", "none")]
    for anthropic_type, expected in table:
        body = areq("or:mock/model", [{"role": "user", "content": "hi"}], tools=tools, tool_choice={"type": anthropic_type})
        status, _, _, conn = ctx.call("/v1/messages", body)
        conn.close()
        ctx.check(f"tool_choice {anthropic_type} -> 200, got {status}", status == 200)
        up_choice = ctx.mock.requests[-1]["body"].get("tool_choice")
        ctx.check(f"tool_choice {{'type':{anthropic_type!r}}} -> {expected!r}, got {up_choice!r}", up_choice == expected)

    body = areq("or:mock/model", [{"role": "user", "content": "hi"}], tools=tools, tool_choice={"type": "tool", "name": "Read"})
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"tool_choice pinned to a tool name -> 200, got {status}", status == 200)
    up_choice = ctx.mock.requests[-1]["body"].get("tool_choice")
    ctx.check(f"tool_choice {{type:tool,name:Read}} -> pinned function form, got {up_choice!r}",
               isinstance(up_choice, dict) and up_choice.get("type") == "function" and up_choice.get("function", {}).get("name") == "Read")


@test
def test_provider_require_parameters_present_when_tools(ctx: Ctx):
    ctx.require_bridge()
    body = areq("or:mock/model", [{"role": "user", "content": "hi"}], tools=[tool_def("Read")])
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"request with tools -> 200, got {status}", status == 200)
    up = ctx.mock.requests[-1]["body"]
    provider = up.get("provider") or {}
    ctx.check(f"provider.require_parameters is True when tools are present (OpenRouter extra), got {provider!r}", provider.get("require_parameters") is True)


@test
def test_defer_loading_tools_dropped_unless_referenced(ctx: Ctx):
    ctx.require_bridge()
    tools = [
        tool_def("core_tool"),
        tool_def("deferred_a", defer_loading=True),
        tool_def("deferred_b", defer_loading=True),
        tool_def("deferred_c_never_referenced", defer_loading=True),
    ]
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_ts", "name": "ToolSearch", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_ts", "content": [
                tool_reference("deferred_a", key="tool_name"),
                tool_reference("deferred_b", key="name"),
            ]},
        ]},
    ]
    body = areq("or:mock/model", messages, tools=tools)
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"defer_loading + tool_reference -> 200, got {status}", status == 200)
    up = ctx.mock.requests[-1]["body"]
    up_tools = up.get("tools") or []
    names = {((t.get("function") or {}).get("name")) for t in up_tools}
    ctx.check(f"core_tool (not deferred) always present, got {names!r}", "core_tool" in names)
    ctx.check(f"deferred_a referenced via 'tool_name' key -> present, got {names!r}", "deferred_a" in names)
    ctx.check(f"deferred_b referenced via 'name' key -> present, got {names!r}", "deferred_b" in names)
    ctx.check(f"deferred_c, never referenced, stays excluded, got {names!r}", "deferred_c_never_referenced" not in names)
    for t in up_tools:
        ctx.check(f"defer_loading marker stripped from forwarded tool def, got {t!r}",
                   "defer_loading" not in t and "defer_loading" not in (t.get("function") or {}))


@test
def test_web_search_tool_returns_400_no_upstream_call(ctx: Ctx):
    ctx.require_bridge()
    before = len(ctx.mock.requests)
    tools = [{"type": "web_search_20250305", "name": "web_search"}]
    body = areq("or:mock/model", [{"role": "user", "content": "search for something"}], tools=tools)
    status, _, payload, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"web_search_20250305 tool -> 400, got {status}: {payload!r}", status == 400)
    msg = ((payload or {}).get("error") or {}).get("message", "") if isinstance(payload, dict) else ""
    ctx.check(f"error message mentions web search being unavailable, got {msg!r}",
               "web search" in msg.lower() and "unavailable" in msg.lower())
    ctx.check("no upstream call was made for the rejected web_search request", len(ctx.mock.requests) == before)


# ---- Group F: multi-session aliasing + tool-cap ----------------------------


@test
def test_two_concurrent_sessions_different_bridge_headers(ctx: Ctx):
    """Two overlapping requests for a tier alias (e.g. 'sonnet') carrying
    different x-bridge-main headers must resolve independently -- proves
    aliasing is per-request, not a shared mutable server-side default that
    would race under concurrent launches."""
    ctx.require_bridge()
    results: dict = {}
    errors: dict = {}

    def run(key, header_value):
        try:
            body = areq("claude-sonnet-4-5-20250929", [{"role": "user", "content": "hi"}])
            status, _, events, conn = ctx.call("/v1/messages", body, headers={"x-bridge-main": header_value}, timeout=10)
            conn.close()
            results[key] = (status, events)
        except Exception as e:  # noqa: BLE001 -- captured for the assertion below
            errors[key] = e

    ta = threading.Thread(target=run, args=("a", "or:mock/session-a"))
    tb = threading.Thread(target=run, args=("b", "or:mock/session-b"))
    ta.start()
    time.sleep(0.05)  # let A (which sleeps 0.3s server-side) be genuinely in-flight before B starts
    tb.start()
    ta.join(timeout=15)
    tb.join(timeout=15)

    ctx.check(f"no exceptions in either concurrent request thread, got {errors!r}", not errors)
    if not errors and "a" in results and "b" in results:
        status_a, events_a = results["a"]
        status_b, events_b = results["b"]
        ctx.check(f"session A -> 200, got {status_a}", status_a == 200)
        ctx.check(f"session B -> 200, got {status_b}", status_b == 200)
        validate_anthropic_stream(events_a)
        validate_anthropic_stream(events_b)
        msg_a, msg_b = assemble_message(events_a), assemble_message(events_b)
        ctx.check(f"session A's own x-bridge-main header resolved independently, got {text_of(msg_a)!r}", text_of(msg_a) == "reply-from-session-a")
        ctx.check(f"session B's own x-bridge-main header resolved independently, got {text_of(msg_b)!r}", text_of(msg_b) == "reply-from-session-b")


@test
def test_150_tools_140_deferred_128_cap_holds(ctx: Ctx):
    ctx.require_bridge()
    core = [tool_def(f"core_{i}") for i in range(10)]
    deferred = [tool_def(f"defer_{i}", defer_loading=True) for i in range(140)]
    tools = core + deferred
    ctx.check(f"constructed exactly 150 tools (10 core + 140 deferred), got {len(tools)}", len(tools) == 150)

    ref_blocks = [tool_reference(f"defer_{i}") for i in range(135)]
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_ts", "name": "ToolSearch", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_ts", "content": ref_blocks}]},
    ]
    body = areq("or:mock/model", messages, tools=tools)
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"150 tools / 140 deferred / 135 referenced -> 200, got {status}", status == 200)
    up_tools = ctx.mock.requests[-1]["body"].get("tools") or []
    ctx.check(f"the 128-tool hard cap holds even though 10+135=145 would otherwise qualify, got {len(up_tools)}", len(up_tools) <= 128)
    names = {((t.get("function") or {}).get("name")) for t in up_tools}
    core_names = {f"core_{i}" for i in range(10)}
    ctx.check(f"all 10 non-deferred core tools survive the cap (priority over deferred-but-referenced), missing={core_names - names!r}",
               core_names.issubset(names))
    deferred_kept = names - core_names
    referenced_set = {f"defer_{i}" for i in range(135)}
    ctx.check(f"every deferred tool kept comes from the referenced set, unexpected={deferred_kept - referenced_set!r}",
               deferred_kept.issubset(referenced_set))
    ctx.check(f"total forwarded is exactly the cap (128), since 145 tools were eligible, got {len(up_tools)}", len(up_tools) == 128)


# ---- Group G: launcher (bridge.py launch), own isolated fixture -----------


@test
def test_launcher_full_contract(ctx: Ctx):
    """Review section 4, test 12. Uses its own LauncherCtx (not the shared
    server/mock) because `launch` spawns a DETACHED background server as a
    side effect -- that persistence-across-invocations is exactly what the
    'live server is reused' assertion below is testing."""
    ctx.require_bridge_file_only()
    lc = LauncherCtx()
    try:
        # --- Launch 1: normal run -- inspect settings file + env + exit code ---
        proc = lc.run_launch(model="or:mock/model", claude_args=["-p", "hi"], exit_code=7)
        ctx.check(f"launcher propagates the fake claude's exit code (7), got {proc.returncode} (stderr: {proc.stderr[-500:] if proc.stderr else ''!r})",
                   proc.returncode == 7)
        dump = lc.read_dump()
        argv = dump["argv"]
        proc_env = dump["env"]  # the fake claude's OWN OS-level environment (deliberately NOT where ANTHROPIC_* lives)

        settings_positions = [i for i, a in enumerate(argv) if a == "--settings"]
        settings_eq = [a for a in argv if isinstance(a, str) and a.startswith("--settings=")]
        total_settings = len(settings_positions) + len(settings_eq)
        ctx.check(f"exactly one --settings flag in argv, got {argv!r}", total_settings == 1)
        # The fake claude process itself read --settings's file and embedded its
        # content in the dump, BEFORE bridge.py gets a chance to clean it up
        # once its child (the fake claude) exits -- reading it back from disk
        # here, after the subprocess call already returned, would race that
        # cleanup and had in fact already been observed to lose the race.
        settings_json = dump.get("settings_content")
        ctx.check(f"--settings JSON has a 'model' key, got {settings_json!r}", isinstance(settings_json, dict) and "model" in settings_json)

        # The ANTHROPIC_* overrides live in the --settings file's own "env" block
        # (that's the whole point of using --settings rather than process env --
        # see plan doc: "--settings merges key-by-key above user/project settings").
        # The fake claude's raw OS environment (proc_env) is deliberately NOT where
        # these should be -- bridge.py's Process-env note explicitly limits direct
        # Popen(env=...) overrides to loopback-only NO_PROXY.
        settings_env = (settings_json or {}).get("env") or {}
        ctx.check(f"settings.env.ANTHROPIC_BASE_URL points at the bridge's own loopback port, got {settings_env.get('ANTHROPIC_BASE_URL')!r}",
                   settings_env.get("ANTHROPIC_BASE_URL") == f"http://127.0.0.1:{lc.port}")

        token_on_disk = lc.token()
        ctx.check(f"settings.env.ANTHROPIC_AUTH_TOKEN matches the per-launch token the bridge wrote to disk, got {settings_env.get('ANTHROPIC_AUTH_TOKEN')!r} vs {token_on_disk!r}",
                   bool(token_on_disk) and settings_env.get("ANTHROPIC_AUTH_TOKEN") == token_on_disk)

        ctx.check(f"settings.env.ENABLE_TOOL_SEARCH == 'true' (required so an openai-chat ref doesn't get all 250+ tools every turn), got {settings_env.get('ENABLE_TOOL_SEARCH')!r}",
                   settings_env.get("ENABLE_TOOL_SEARCH") == "true")

        custom_headers = settings_env.get("ANTHROPIC_CUSTOM_HEADERS", "") or ""
        ctx.check(f"settings.env.ANTHROPIC_CUSTOM_HEADERS contains 'x-bridge-main: or:mock/model', got {custom_headers!r}",
                   "x-bridge-main: or:mock/model" in custom_headers)

        first_server_info = lc.server_json()
        ctx.check(f"server.json exists with an integer pid after the first launch, got {first_server_info!r}",
                   isinstance(first_server_info, dict) and isinstance(first_server_info.get("pid"), int))

        # --- Launch 2: same port/state -> must reuse the already-live server ---
        proc2 = lc.run_launch(model="or:mock/model", claude_args=["-p", "hi again"], exit_code=9)
        ctx.check(f"second launch also propagates ITS exit code (9) rather than the first's, got {proc2.returncode}", proc2.returncode == 9)
        second_server_info = lc.server_json()
        if isinstance(first_server_info, dict) and isinstance(second_server_info, dict):
            ctx.check(f"the second launch reused the SAME server pid rather than spawning a new one, got {first_server_info.get('pid')!r} vs {second_server_info.get('pid')!r}",
                       first_server_info.get("pid") == second_server_info.get("pid"))

        # --- Launch 3: a pre-seeded settings.json 'model' key must survive the child hijacking it ---
        proc3 = lc.run_launch(model="or:mock/model", claude_args=["-p", "hi"], exit_code=7, hijack_settings=True)
        ctx.check(f"third launch (settings hijack) still propagates exit code 7, got {proc3.returncode}", proc3.returncode == 7)
        restored = json.loads(lc.settings_path.read_text(encoding="utf-8"))
        ctx.check(f"~/.claude/settings.json's pre-existing 'model' key is restored after exit despite the child rewriting it mid-run, got {restored!r}",
                   restored.get("model") == "sonnet-preexisting")
    finally:
        lc.stop()


@test
def test_launcher_merges_process_env_custom_headers(ctx: Ctx):
    """finding 7: a shell-exported ANTHROPIC_CUSTOM_HEADERS must be merged
    into the settings file's env, not silently dropped just because the
    settings chain didn't also define one -- the settings chain wins on a
    name collision, and bridge's own x-bridge-* lines are still appended."""
    ctx.require_bridge_file_only()
    lc = LauncherCtx()
    try:
        lc.settings_path.write_text(json.dumps({
            "model": "sonnet-preexisting",
            "env": {"ANTHROPIC_CUSTOM_HEADERS": "x-databricks-use-coding-agent-mode: from-chain"},
        }), encoding="utf-8")
        proc = lc.run_launch(model="or:mock/model", claude_args=["-p", "hi"], exit_code=7,
                              extra_env={"ANTHROPIC_CUSTOM_HEADERS":
                                         "x-databricks-use-coding-agent-mode: from-shell\nx-shell-only: shell-value"})
        ctx.check(f"launch exit 7, got {proc.returncode}", proc.returncode == 7)
        dump = lc.read_dump()
        settings_env = (dump.get("settings_content") or {}).get("env") or {}
        headers = settings_env.get("ANTHROPIC_CUSTOM_HEADERS", "")
        ctx.check(f"settings-chain value wins the name collision, got {headers!r}",
                   "x-databricks-use-coding-agent-mode: from-chain" in headers and "from-shell" not in headers)
        ctx.check(f"a non-conflicting shell-only header still survives, got {headers!r}",
                   "x-shell-only: shell-value" in headers)
        ctx.check(f"bridge's own x-bridge-main is still appended, got {headers!r}",
                   "x-bridge-main: or:mock/model" in headers)
    finally:
        lc.stop()


@test
def test_launcher_dbx_claude_without_databricks_prefix_is_passthrough(ctx: Ctx):
    """finding 10: route_model sends ANY dbx:-prefixed name containing
    "claude" to the passthrough dialect, not just databricks-*/system.ai.*
    ones -- is_passthrough_ref must classify a bare `dbx:claude-sonnet-4-5`
    ref the same way, or the launcher wrongly sets MAX_THINKING_TOKENS=0 and
    force-enables tool search on what the server treats as a raw Claude
    relay, and forces the openai-chat 128000/16384 caps onto a real Claude
    session."""
    ctx.require_bridge_file_only()
    lc = LauncherCtx()
    try:
        proc = lc.run_launch(model="dbx:claude-sonnet-4-5", claude_args=["-p", "hi"], exit_code=7)
        ctx.check(f"launch exit 7, got {proc.returncode}", proc.returncode == 7)
        dump = lc.read_dump()
        settings_env = (dump.get("settings_content") or {}).get("env") or {}
        ctx.check(f"MAX_THINKING_TOKENS not forced to '0' for a dbx:claude-* ref with no databricks-/"
                   f"system.ai. prefix, got {settings_env.get('MAX_THINKING_TOKENS')!r}",
                   settings_env.get("MAX_THINKING_TOKENS") != "0")
        ctx.check(f"CLAUDE_CODE_MAX_CONTEXT_TOKENS omitted (no models.json entry) rather than forced to "
                   f"the openai-chat default, got {settings_env.get('CLAUDE_CODE_MAX_CONTEXT_TOKENS')!r}",
                   "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in settings_env)
    finally:
        lc.stop()


@test
def test_launcher_bare_model_colon_in_openrouter_id_not_mistaken_for_prefix(ctx: Ctx):
    """finding 10: the bare-model derivation must strip only a LEADING
    dbx:/or: prefix, not split on the first ':' anywhere -- an OpenRouter
    model id like "qwen/qwen3-coder:free" has its own colon that must
    survive intact for the models.json lookup key."""
    ctx.require_bridge_file_only()
    lc = LauncherCtx()
    try:
        models_json = lc.state_dir / "models.json"
        models_json.parent.mkdir(parents=True, exist_ok=True)
        models_json.write_text(json.dumps({
            "qwen/qwen3-coder:free": {"context_length": 111111, "max_output_tokens": 2222},
            "free": {"context_length": 999, "max_output_tokens": 999},
        }), encoding="utf-8")
        proc = lc.run_launch(model="or:qwen/qwen3-coder:free", claude_args=["-p", "hi"], exit_code=7)
        ctx.check(f"launch exit 7, got {proc.returncode}", proc.returncode == 7)
        dump = lc.read_dump()
        settings_env = (dump.get("settings_content") or {}).get("env") or {}
        ctx.check(f"CLAUDE_CODE_MAX_CONTEXT_TOKENS uses the FULL model id's entry (111111), not the "
                   f"'free' suffix's (999), got {settings_env.get('CLAUDE_CODE_MAX_CONTEXT_TOKENS')!r}",
                   settings_env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") == "111111")
    finally:
        lc.stop()


@test
def test_launcher_argv_bridge_flags_no_separator(ctx: Ctx):
    """finding 14: `launch --model X -p hi` (no "--" at all) must let claude
    see `-p hi` while the bridge itself uses model X. The old code only ever
    recognized bridge flags before the FIRST literal "--" in the whole argv
    -- exactly what made a wrapper that no longer inserts one (the fix for
    the SAME finding) forward `--model X` to claude instead of the bridge."""
    ctx.require_bridge_file_only()
    lc = LauncherCtx()
    try:
        proc = lc.run_launch(exit_code=7,
                              raw_launch_args=["--model", "or:mock/model", "--port", str(lc.port), "-p", "hi"])
        ctx.check(f"launch exit 7, got {proc.returncode} (stderr: {proc.stderr[-500:] if proc.stderr else ''!r})",
                   proc.returncode == 7)
        dump = lc.read_dump()
        ctx.check(f"claude sees -p hi untouched, got argv {dump['argv']!r}", dump["argv"][-2:] == ["-p", "hi"])
        settings_json = dump.get("settings_content") or {}
        ctx.check(f"settings model == or:mock/model (the bridge's own --model, not forwarded to claude), "
                   f"got {settings_json.get('model')!r}", settings_json.get("model") == "or:mock/model")
    finally:
        lc.stop()


@test
def test_launcher_argv_no_model_flag_uses_default_ref(ctx: Ctx):
    """finding 14: `launch -p hi` (no --model at all) must fall back to the
    default ref chain (here BRIDGE_MODEL), not error out or leave the
    bridge's own settings model empty."""
    ctx.require_bridge_file_only()
    lc = LauncherCtx()
    try:
        proc = lc.run_launch(exit_code=7, extra_env={"BRIDGE_MODEL": "or:mock/model"},
                              raw_launch_args=["--port", str(lc.port), "-p", "hi"])
        ctx.check(f"launch exit 7, got {proc.returncode} (stderr: {proc.stderr[-500:] if proc.stderr else ''!r})",
                   proc.returncode == 7)
        dump = lc.read_dump()
        ctx.check(f"claude sees -p hi untouched, got argv {dump['argv']!r}", dump["argv"][-2:] == ["-p", "hi"])
        settings_json = dump.get("settings_content") or {}
        ctx.check(f"settings model falls back to BRIDGE_MODEL, got {settings_json.get('model')!r}",
                   settings_json.get("model") == "or:mock/model")
    finally:
        lc.stop()


@test
def test_launcher_argv_explicit_separator_lets_user_model_reach_claude(ctx: Ctx):
    """finding 14: `launch --model X -- --model Y` must still forward
    `--model Y` to claude untouched, with the bridge itself using X."""
    ctx.require_bridge_file_only()
    lc = LauncherCtx()
    try:
        proc = lc.run_launch(exit_code=7, raw_launch_args=[
            "--model", "or:mock/model", "--port", str(lc.port), "--", "--model", "Y",
        ])
        ctx.check(f"launch exit 7, got {proc.returncode} (stderr: {proc.stderr[-500:] if proc.stderr else ''!r})",
                   proc.returncode == 7)
        dump = lc.read_dump()
        ctx.check(f"claude sees --model Y untouched, got argv {dump['argv']!r}", dump["argv"][-2:] == ["--model", "Y"])
        settings_json = dump.get("settings_content") or {}
        ctx.check(f"bridge's own settings model is still or:mock/model, got {settings_json.get('model')!r}",
                   settings_json.get("model") == "or:mock/model")
    finally:
        lc.stop()


@test
def test_launcher_routes_json_default_and_small_consulted(ctx: Ctx):
    """finding 15: routes.json's `default`/`small` fields were accepted
    (even documented) but no code path ever actually read them -- only
    `profiles` was wired up."""
    ctx.require_bridge_file_only()
    lc = LauncherCtx()
    try:
        routes_path = lc.state_dir / "routes.json"
        routes_path.parent.mkdir(parents=True, exist_ok=True)
        routes_path.write_text(json.dumps({"default": "or:mock/model", "small": "or:mock/small"}), encoding="utf-8")
        proc = lc.run_launch(exit_code=7, raw_launch_args=["--port", str(lc.port), "-p", "hi"])
        ctx.check(f"launch exit 7, got {proc.returncode} (stderr: {proc.stderr[-500:] if proc.stderr else ''!r})",
                   proc.returncode == 7)
        dump = lc.read_dump()
        settings_json = dump.get("settings_content") or {}
        ctx.check(f"settings model comes from routes.json's 'default', got {settings_json.get('model')!r}",
                   settings_json.get("model") == "or:mock/model")
        settings_env = settings_json.get("env") or {}
        ctx.check(f"ANTHROPIC_DEFAULT_HAIKU_MODEL comes from routes.json's 'small', got "
                   f"{settings_env.get('ANTHROPIC_DEFAULT_HAIKU_MODEL')!r}",
                   settings_env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL") == "or:mock/small")
    finally:
        lc.stop()


@test
def test_launcher_garbage_http_listener_treated_as_absent_then_busy_port_fails_fast(ctx: Ctx):
    """finding 9: check_server_status only caught OSError -- a listener that
    accepts the TCP connection but replies with a garbage (unparseable)
    HTTP status line raises http.client.BadStatusLine, an HTTPException
    subclass, NOT an OSError, straight through the old code and crashed
    `launch` with a raw traceback. It must be classified 'absent' instead;
    `launch` must then notice the port is STILL not actually free and fail
    fast, rather than wasting the whole 10s healthz-poll timeout trying to
    spawn a second server on an occupied port."""
    ctx.require_bridge_file_only()
    lc = LauncherCtx()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    stop_flag = threading.Event()
    server_thread = None
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", lc.port))
        listener.listen(5)
        listener.settimeout(0.5)

        def serve():
            while not stop_flag.is_set():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                try:
                    conn.recv(4096)
                    conn.sendall(b"not a valid http status line at all\r\n\r\n")
                except OSError:
                    pass
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()

        proc = lc.run_launch(model="or:mock/model", claude_args=["-p", "hi"], exit_code=7, timeout=20.0)
        # (BridgeStartError propagating as an uncaught exception -- and thus a
        # Python traceback on stderr -- is pre-existing, unrelated behavior;
        # what finding 9 actually fixes is that check_server_status doesn't
        # crash entirely on a garbage HTTP response, and that `launch` then
        # refuses to spawn a second server rather than trying anyway.)
        ctx.check(f"launch against a garbage-HTTP listener still exits non-zero rather than hanging or "
                   f"succeeding, got rc={proc.returncode}", proc.returncode != 0)
        ctx.check(f"stderr names the busy port {lc.port}, got {proc.stderr[-500:] if proc.stderr else ''!r}",
                   str(lc.port) in (proc.stderr or ""))
    finally:
        stop_flag.set()
        try:
            listener.close()
        except OSError:
            pass
        if server_thread is not None:
            server_thread.join(timeout=2)
        lc.stop()


@test
def test_dbx_mirrored_headers_logged_at_info(ctx: Ctx):
    """finding 7: bridge.log must show which custom header names were
    mirrored upstream, or the work acceptance line ("bridge.log shows the
    mirrored header") can never be confirmed."""
    ctx.require_dbx_bridge()
    def handler(h, body):
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "ok"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])
    ctx.dbx_mock.handlers["/serving-endpoints/databricks-log-case/invocations"] = handler
    body = areq("dbx:databricks-log-case", [{"role": "user", "content": "hi"}])
    status, _, _, conn = ctx.dbx_call("/v1/messages", body, headers={
        "X-Databricks-Use-Coding-Agent-Mode": "true",
    })
    conn.close()
    ctx.check(f"request -> 200, got {status}", status == 200)
    log_path = ctx.dbx_bridge.state_dir / "bridge.log"
    deadline = time.monotonic() + 3
    log_text = ""
    while time.monotonic() < deadline:
        if log_path.exists():
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            if "x-databricks-use-coding-agent-mode" in log_text.lower():
                break
        time.sleep(0.1)
    ctx.check(f"bridge.log records the mirrored header name, got tail: {log_text[-800:]!r}",
               "x-databricks-use-coding-agent-mode" in log_text.lower())


# ---- Group H: Databricks openai-chat route (M4) -------------------------




@test
def test_dbx_invocations_route_no_model_field(ctx: Ctx):
    ctx.require_dbx_bridge()
    def handler(h, body):
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "pong from invocations"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])
    ctx.dbx_mock.handlers["/serving-endpoints/databricks-route-basic/invocations"] = handler
    body = areq("dbx:databricks-route-basic", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.dbx_call("/v1/messages", body)
    conn.close()
    ctx.check(f"invocations route -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    ctx.check(f"text == 'pong from invocations', got {text_of(msg)!r}", text_of(msg) == "pong from invocations")
    last_req = ctx.dbx_mock.requests[-1]
    ctx.check(f"invocations path uses the FULL model name, prefix included (finding 8), got {last_req['path']!r}",
               last_req["path"] == "/serving-endpoints/databricks-route-basic/invocations")
    body_sent = last_req["body"]
    ctx.check(f"no 'model' field in forwarded body, got keys {list(body_sent.keys())!r}", "model" not in body_sent)



@test
def test_dbx_mlflow_route_has_model_field(ctx: Ctx):
    ctx.require_dbx_bridge()
    def handler(h, body):
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "pong from mlflow"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])
    ctx.dbx_mock.handlers["/ai-gateway/mlflow/v1/chat/completions"] = handler
    body = areq("dbx:system.ai.route-mlflow", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.dbx_call("/v1/messages", body)
    conn.close()
    ctx.check(f"mlflow route -> 200, got {status}", status == 200)
    last_req = ctx.dbx_mock.requests[-1]
    body_sent = last_req["body"]
    ctx.check(f"forwarded body contains 'model' field, got {body_sent.get('model')!r}", body_sent.get("model") == "system.ai.route-mlflow")



@test
def test_dbx_404_fallback_then_cached(ctx: Ctx):
    ctx.require_dbx_bridge()
    def handler(h, body):
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "fallback worked"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])
    ctx.dbx_mock.handlers["/ai-gateway/mlflow/v1/chat/completions"] = handler
    # No handler for /serving-endpoints/databricks-fallback-case/invocations -> auto-404
    ctx.dbx_mock.clear()
    body = areq("dbx:databricks-fallback-case", [{"role": "user", "content": "hi"}])
    status1, _, events1, conn1 = ctx.dbx_call("/v1/messages", body)
    conn1.close()
    ctx.check(f"first request -> 200, got {status1}", status1 == 200)
    paths = [r["path"] for r in ctx.dbx_mock.requests]
    ctx.check(f"first request tried invocations then mlflow, got {paths!r}", paths == [
        "/serving-endpoints/databricks-fallback-case/invocations",
        "/ai-gateway/mlflow/v1/chat/completions",
    ])
    ctx.dbx_mock.clear()
    status2, _, events2, conn2 = ctx.dbx_call("/v1/messages", body)
    conn2.close()
    ctx.check(f"second request -> 200, got {status2}", status2 == 200)
    paths2 = [r["path"] for r in ctx.dbx_mock.requests]
    ctx.check(f"second request only mlflow (cached), got {paths2!r}", paths2 == ["/ai-gateway/mlflow/v1/chat/completions"])

    # finding 12: the cache must actually be persisted to disk, not just kept
    # in memory (the whole point is surviving a server restart).
    cache_path = ctx.dbx_bridge.state_dir / "routes-cache.json"
    deadline = time.monotonic() + 3
    disk_cache = {}
    while time.monotonic() < deadline:
        if cache_path.exists():
            try:
                disk_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                disk_cache = {}
            if "databricks-fallback-case" in disk_cache:
                break
        time.sleep(0.1)
    ctx.check(f"routes-cache.json on disk records the mlflow route (index 1) for this model, got {disk_cache!r}",
               isinstance(disk_cache.get("databricks-fallback-case"), dict)
               and disk_cache["databricks-fallback-case"].get("route") == 1)



@test
def test_dbx_body_allowlist_drops_provider_keeps_tools(ctx: Ctx):
    ctx.require_dbx_bridge()
    def handler(h, body):
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "ok"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])
    ctx.dbx_mock.handlers["/serving-endpoints/databricks-allowlist-case/invocations"] = handler
    body = areq("dbx:databricks-allowlist-case", [{"role": "user", "content": "hi"}],
                tools=[tool_def("Read")])
    status, _, events, conn = ctx.dbx_call("/v1/messages", body)
    conn.close()
    ctx.check(f"tools request -> 200, got {status}", status == 200)
    last_body = ctx.dbx_mock.requests[-1]["body"]
    allowed = {"messages", "max_tokens", "temperature", "top_p", "stop", "stream", "tools", "tool_choice"}
    extra_keys = set(last_body.keys()) - allowed
    ctx.check(f"no extra keys beyond allowlist, got {extra_keys!r}", extra_keys == set())
    ctx.check(f"'tools' present, got {last_body.get('tools')!r}", "tools" in last_body)
    ctx.check(f"'provider' absent, got {last_body.get('provider')!r}", "provider" not in last_body)
    ctx.check(f"'model' absent, got {last_body.get('model')!r}", "model" not in last_body)
    ctx.check(f"'usage' absent, got {last_body.get('usage')!r}", "usage" not in last_body)
    ctx.check(f"'reasoning' absent, got {last_body.get('reasoning')!r}", "reasoning" not in last_body)



@test
def test_dbx_reasoning_parts_flattened_not_emitted(ctx: Ctx):
    ctx.require_dbx_bridge()
    def handler(h, body):
        start_sse(h)
        write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
        write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"content": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "internal chain of thought " * 40}]},
            {"type": "text", "text": "final answer"},
        ]}}]})
        write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        write_sse_chunk(h, None, "[DONE]")
        end_sse(h)
    ctx.dbx_mock.handlers["/serving-endpoints/databricks-reasoning-case/invocations"] = handler
    body = areq("dbx:databricks-reasoning-case", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.dbx_call("/v1/messages", body)
    conn.close()
    ctx.check(f"reasoning parts -> 200, got {status}", status == 200)
    validate_anthropic_stream(events)
    msg = assemble_message(events)
    ctx.check(f"text == 'final answer', got {text_of(msg)!r}", text_of(msg) == "final answer")
    events_json = json.dumps(events)
    ctx.check("'internal chain of thought' substring appears in NONE of the events", "internal chain of thought" not in events_json)



@test
def test_dbx_custom_headers_mirrored_bridge_headers_stripped(ctx: Ctx):
    ctx.require_dbx_bridge()
    def handler(h, body):
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "ok"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])
    ctx.dbx_mock.handlers["/serving-endpoints/databricks-headers-case/invocations"] = handler
    body = areq("dbx:databricks-headers-case", [{"role": "user", "content": "hi"}])
    status, _, events, conn = ctx.dbx_call("/v1/messages", body, headers={
        "X-Databricks-Use-Coding-Agent-Mode": "true",
        "x-bridge-main": "should-not-leak",
        "x-api-key": ctx.dbx_bridge.token,
    })
    conn.close()
    ctx.check(f"headers request -> 200, got {status}", status == 200)
    last_req = ctx.dbx_mock.requests[-1]
    hdr = last_req["headers"]
    ctx.check(f"x-databricks-use-coding-agent-mode mirrored, got {hdr.get('x-databricks-use-coding-agent-mode')!r}",
               hdr.get("x-databricks-use-coding-agent-mode") == "true")
    ctx.check(f"no header starting with x-bridge- (case-insensitive), got {[k for k in hdr if k.startswith('x-bridge-')]!r}",
               not any(k.startswith("x-bridge-") for k in hdr))
    ctx.check(f"client's own x-api-key never forwarded, got {hdr.get('x-api-key')!r}", "x-api-key" not in hdr)



@test
def test_dbx_max_tokens_400_clamp_retry(ctx: Ctx):
    """Real Databricks serving-endpoint 400 shape is {"error_code","message"}
    -- no nested "error" object at all (finding 2/12)."""
    ctx.require_dbx_bridge()
    def handler(h, body):
        max_tokens = (body or {}).get("max_tokens", 0)
        if max_tokens > 128:
            send_json_response(h, 400, {"error_code": "BAD_REQUEST", "message": "max_tokens must be <= 128"})
        else:
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"content": "clamped retry succeeded"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])
    ctx.dbx_mock.handlers["/serving-endpoints/databricks-maxtok-case/invocations"] = handler
    body = areq("dbx:databricks-maxtok-case", [{"role": "user", "content": "hi"}], max_tokens=99999)
    status, _, events, conn = ctx.dbx_call("/v1/messages", body)
    conn.close()
    ctx.check(f"max_tokens 400 -> clamped retry -> 200, got {status}", status == 200)
    msg = assemble_message(events)
    ctx.check(f"text == 'clamped retry succeeded', got {text_of(msg)!r}", text_of(msg) == "clamped retry succeeded")


@test
def test_dbx_max_tokens_cached_limit_reused_upfront(ctx: Ctx):
    """finding 2: dbx_cache_get_max_tokens_limit had no caller -- a
    previously-discovered ceiling for this model must be consumed in the
    up-front clamp on later requests, so they don't pay the same 400 +
    retry round trip every single time."""
    ctx.require_dbx_bridge()
    calls = []
    def handler(h, body):
        max_tokens = (body or {}).get("max_tokens", 0)
        calls.append(max_tokens)
        if max_tokens > 200:
            send_json_response(h, 400, {"error_code": "BAD_REQUEST", "message": "max_tokens must be <= 200"})
        else:
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"content": "ok"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])
    ctx.dbx_mock.handlers["/serving-endpoints/databricks-cache-reuse-case/invocations"] = handler
    body = areq("dbx:databricks-cache-reuse-case", [{"role": "user", "content": "hi"}], max_tokens=99999)
    status1, _, _, conn1 = ctx.dbx_call("/v1/messages", body)
    conn1.close()
    ctx.check(f"first call -> 200 after clamp-retry, got {status1}", status1 == 200)
    ctx.check(f"first call needed a 400 + retry (2 upstream calls), got {calls!r}", len(calls) == 2)

    calls.clear()
    body2 = areq("dbx:databricks-cache-reuse-case", [{"role": "user", "content": "hi again"}], max_tokens=99999)
    status2, _, _, conn2 = ctx.dbx_call("/v1/messages", body2)
    conn2.close()
    ctx.check(f"second call -> 200, got {status2}", status2 == 200)
    ctx.check(f"second call reuses the cached limit up front -- exactly ONE upstream call, got {calls!r}",
               len(calls) == 1 and calls[0] <= 200)


@test
def test_dbx_dns_failure_502_vpn_hint(ctx: Ctx):
    ctx.require_bridge_file_only()
    bridge = BridgeProc(ctx.mock, extra_env={
        "BRIDGE_DBX_BASE_URL": "https://bridge-test-does-not-exist.invalid",
        "BRIDGE_DBX_TOKEN": "x",
    })
    try:
        bridge.start()
        body = areq("dbx:databricks-anything", [{"role": "user", "content": "hi"}])
        status, headers, payload, conn = http_call(bridge.port, "POST", "/v1/messages", body=body,
                                                    headers={"x-api-key": bridge.token})
        conn.close()
        ctx.check(f"DNS failure -> 502, got {status}", status == 502)
        ctx.check(f"x-should-retry header == false, got {headers.get('x-should-retry')!r}",
                   headers.get("x-should-retry") == "false")
        err_msg = ((payload or {}).get("error") or {}).get("message", "") if isinstance(payload, dict) else ""
        ctx.check(f"error message ends with VPN hint substring, got {err_msg!r}",
                   err_msg.endswith("(are you on the VPN? Databricks is whitelisted)"))
    finally:
        bridge.stop()

# ---- Group I: Databricks Claude passthrough (M5) -------------------------




@test
def test_passthrough_byte_relay_status_and_headers(ctx: Ctx):
    ctx.require_dbx_bridge()
    def handler(h, body):
        start_sse(h, status=200, extra_headers={"X-Marker": "relay-ok"})
        write_sse_chunk(h, "message_start", {"type": "message_start", "marker": "XYZ789"})
        end_sse(h)
    ctx.dbx_mock.handlers["/ai-gateway/anthropic/v1/messages"] = handler
    body = areq("dbx:databricks-claude-relay", [{"role": "user", "content": "hi"}])
    status, headers, payload, conn = ctx.dbx_call("/v1/messages", body)
    conn.close()
    ctx.check(f"passthrough -> 200, got {status}", status == 200)
    ctx.check(f"response header X‑Marker present, got {headers.get('x-marker')!r}", headers.get("x-marker") == "relay-ok")
    marker_event = next((e for e in payload if isinstance(e.get("data"), dict) and e["data"].get("marker") == "XYZ789"), None)
    ctx.check(f"raw event with marker XYZ789 relayed, got {marker_event!r}", marker_event is not None)



@test
def test_passthrough_header_swap(ctx: Ctx):
    ctx.require_dbx_bridge()
    def handler(h, body):
        start_sse(h)
        write_sse_chunk(h, "message_stop", {"type": "message_stop"})
        end_sse(h)
    ctx.dbx_mock.handlers["/ai-gateway/anthropic/v1/messages"] = handler
    body = areq("dbx:databricks-claude-headers", [{"role": "user", "content": "hi"}])
    ctx.dbx_call("/v1/messages", body)  # ignore response
    last_req = ctx.dbx_mock.requests[-1]
    hdr = last_req["headers"]
    ctx.check(f"authorization == 'Bearer test-dbx-token', got {hdr.get('authorization')!r}",
               hdr.get("authorization") == "Bearer test-dbx-token")
    ctx.check(f"x-api-key absent, got {hdr.get('x-api-key')!r}", "x-api-key" not in hdr)



@test
def test_passthrough_strips_bridge1_signed_thinking_block(ctx: Ctx):
    ctx.require_dbx_bridge()
    recorded = []
    def handler(h, body):
        recorded.append(body)
        start_sse(h)
        write_sse_chunk(h, "message_stop", {"type": "message_stop"})
        end_sse(h)
    ctx.dbx_mock.handlers["/ai-gateway/anthropic/v1/messages"] = handler
    body = areq("dbx:databricks-claude-thinking", messages=[
        {"role": "user", "content": "think"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "fake", "signature": "bridge1.deadbeef"},
            {"type": "text", "text": "normal answer"},
        ]},
    ])
    ctx.dbx_call("/v1/messages", body)  # ignore response
    ctx.check("mock received a body", len(recorded) == 1)
    if recorded:
        messages = recorded[0].get("messages", [])
        asst_msg = next((m for m in messages if m.get("role") == "assistant"), None)
        ctx.check("assistant message exists", asst_msg is not None)
        if asst_msg:
            content = asst_msg.get("content", [])
            ctx.check("no thinking block in relayed content", not any(isinstance(b, dict) and b.get("type") == "thinking" for b in content))
            ctx.check("text block survives", any(isinstance(b, dict) and b.get("type") == "text" for b in content))



@test
def test_passthrough_beta_header_filters_tool_search_keeps_others(ctx: Ctx):
    ctx.require_dbx_bridge()
    recorded = []
    def handler(h, body):
        recorded.append(h.headers)
        start_sse(h)
        write_sse_chunk(h, "message_stop", {"type": "message_stop"})
        end_sse(h)
    ctx.dbx_mock.handlers["/ai-gateway/anthropic/v1/messages"] = handler
    body = areq("dbx:databricks-claude-beta", [{"role": "user", "content": "hi"}])
    ctx.dbx_call("/v1/messages", body, headers={
        "anthropic-beta": "tool-search-tool-2025-01-01, some-other-beta",
    })
    ctx.check("mock received a request", len(recorded) == 1)
    if recorded:
        hdr = recorded[0]
        beta_val = hdr.get("anthropic-beta") or hdr.get("anthropic-beta".lower())
        ctx.check(f"beta header present, got {beta_val!r}", beta_val is not None)
        if beta_val:
            parts = [p.strip() for p in beta_val.split(",")]
            ctx.check("some-other-beta kept", "some-other-beta" in parts)
            ctx.check("tool-search-tool-* filtered out", not any(p.startswith("tool-search-tool") for p in parts))



@test
def test_passthrough_model_rewritten_dbx_prefix_stripped(ctx: Ctx):
    ctx.require_dbx_bridge()
    recorded = []
    def handler(h, body):
        recorded.append(body)
        start_sse(h)
        write_sse_chunk(h, "message_stop", {"type": "message_stop"})
        end_sse(h)
    ctx.dbx_mock.handlers["/ai-gateway/anthropic/v1/messages"] = handler
    body = areq("dbx:databricks-claude-rewrite", [{"role": "user", "content": "hi"}])
    ctx.dbx_call("/v1/messages", body)
    ctx.check("mock received a body", len(recorded) == 1)
    if recorded:
        relayed_model = recorded[0].get("model")
        ctx.check(f"model rewritten to databricks-claude-rewrite (no dbx:), got {relayed_model!r}",
                   relayed_model == "databricks-claude-rewrite")



@test
def test_passthrough_count_tokens_relay_success(ctx: Ctx):
    ctx.require_dbx_bridge()
    def handler(h, body):
        send_json_response(h, 200, {"input_tokens": 4242})
    ctx.dbx_mock.handlers["/ai-gateway/anthropic/v1/messages/count_tokens"] = handler
    body = areq("dbx:databricks-claude-counttok-ok", [{"role": "user", "content": "hi"}])
    status, _, payload, conn = ctx.dbx_call("/v1/messages/count_tokens", body)
    conn.close()
    ctx.check(f"count_tokens relay -> 200, got {status}", status == 200)
    ctx.check(f"input_tokens == 4242, got {payload!r}", isinstance(payload, dict) and payload.get("input_tokens") == 4242)



@test
def test_passthrough_count_tokens_relay_fallback_on_failure(ctx: Ctx):
    ctx.require_dbx_bridge()
    def handler(h, body):
        send_json_response(h, 500, {"error": "boom"})
    ctx.dbx_mock.handlers["/ai-gateway/anthropic/v1/messages/count_tokens"] = handler
    body = areq("dbx:databricks-claude-counttok-fail", [{"role": "user", "content": "hi"}])
    status, _, payload, conn = ctx.dbx_call("/v1/messages/count_tokens", body)
    conn.close()
    ctx.check(f"relay failure -> still 200, got {status}", status == 200)
    ctx.check(f"input_tokens is an int > 0, got {payload!r}",
               isinstance(payload, dict) and isinstance(payload.get("input_tokens"), int) and payload["input_tokens"] > 0)


@test
def test_passthrough_relays_non_2xx_status(ctx: Ctx):
    """finding 12: passthrough has no map_upstream_error/overflow rewriting
    of its own -- Databricks' native Claude endpoint already speaks
    Anthropic's own error shape, so a non-2xx status/body must be relayed to
    the client byte-for-byte, not swallowed or replaced."""
    ctx.require_dbx_bridge()
    def handler(h, body):
        send_json_response(h, 529, {"type": "error", "error": {"type": "overloaded_error", "message": "upstream overloaded"}})
    ctx.dbx_mock.handlers["/ai-gateway/anthropic/v1/messages"] = handler
    body = areq("dbx:databricks-claude-non2xx", [{"role": "user", "content": "hi"}])
    status, _, payload, conn = ctx.dbx_call("/v1/messages", body)
    conn.close()
    ctx.check(f"passthrough relays the real upstream status verbatim (529), got {status}", status == 529)
    ctx.check(f"passthrough relays the real upstream JSON body verbatim, got {payload!r}",
               isinstance(payload, dict) and (payload.get("error") or {}).get("message") == "upstream overloaded")


@test
def test_passthrough_defer_loading_stripped_and_capped(ctx: Ctx):
    """finding 12: passthrough must apply the same defer_loading strip / 128
    tool cap as the openai-chat path (build_passthrough_body already reuses
    select_tools() for this)."""
    ctx.require_dbx_bridge()
    recorded = []
    def handler(h, body):
        recorded.append(body)
        start_sse(h)
        write_sse_chunk(h, "message_stop", {"type": "message_stop"})
        end_sse(h)
    ctx.dbx_mock.handlers["/ai-gateway/anthropic/v1/messages"] = handler
    tools = [tool_def("core_tool"), tool_def("deferred_never_referenced", defer_loading=True)]
    body = areq("dbx:databricks-claude-defer", [{"role": "user", "content": "hi"}], tools=tools)
    ctx.dbx_call("/v1/messages", body)
    ctx.check("mock received a body", len(recorded) == 1)
    if recorded:
        up_tools = recorded[0].get("tools") or []
        names = {t.get("name") for t in up_tools}
        ctx.check(f"core (non-deferred) tool present, got {names!r}", "core_tool" in names)
        ctx.check(f"unreferenced deferred tool excluded, got {names!r}", "deferred_never_referenced" not in names)
        for t in up_tools:
            ctx.check(f"defer_loading key stripped from forwarded tool def, got {t!r}", "defer_loading" not in t)


@test
def test_passthrough_tool_reference_replaced_with_text(ctx: Ctx):
    """finding 5: a tool_reference block (as ToolSearch returns) must not
    reach Databricks' native Claude endpoint verbatim -- it has no
    tool-search-tool-* beta enabled there (we strip it) and 400s on the
    unrecognized block type."""
    ctx.require_dbx_bridge()
    recorded = []
    def handler(h, body):
        recorded.append(body)
        start_sse(h)
        write_sse_chunk(h, "message_stop", {"type": "message_stop"})
        end_sse(h)
    ctx.dbx_mock.handlers["/ai-gateway/anthropic/v1/messages"] = handler
    body = areq("dbx:databricks-claude-toolref", messages=[
        {"role": "user", "content": "search"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_ts", "name": "ToolSearch", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_ts", "content": [
                {"type": "tool_reference", "tool_name": "sd3_search_presets"},
            ]},
        ]},
    ])
    ctx.dbx_call("/v1/messages", body)
    ctx.check("mock received a body", len(recorded) == 1)
    if recorded:
        body_json = json.dumps(recorded[0])
        ctx.check(f"no raw tool_reference block type reaches Databricks, got body: {body_json!r}",
                   "tool_reference" not in body_json)
        ctx.check(f"tool name surfaces as plain text instead, got body: {body_json!r}",
                   "sd3_search_presets" in body_json)


@test
def test_passthrough_error_frame_prefixed_with_blank_line(ctx: Ctx):
    """finding 11: a synthetic mid-stream error frame must be prefixed with a
    blank line so it can't be joined onto a preceding partial (no trailing
    newline) chunk from the dying upstream connection."""
    ctx.require_dbx_bridge()
    def handler(h, body):
        start_sse(h)
        # A fragment that ends mid-line (no trailing CRLF) -- simulates the
        # upstream dying in the middle of writing an event.
        frag = b'event: content_block_delta\r\ndata: {"type": "content_block_delta", "trunc'
        try:
            h.wfile.write(b"%x\r\n%s\r\n" % (len(frag), frag))
            h.wfile.flush()
        except OSError:
            pass
        abrupt_disconnect(h)
    ctx.dbx_mock.handlers["/ai-gateway/anthropic/v1/messages"] = handler
    body = areq("dbx:databricks-claude-deadconn", [{"role": "user", "content": "hi"}])
    status, _, payload, conn = ctx.dbx_call("/v1/messages", body, timeout=10)
    conn.close()
    ctx.check(f"passthrough still starts as 200, got {status}", status == 200)
    ctx.check(f"a cleanly-named 'error' SSE event is still parseable after the mid-event death "
               f"(not joined onto the truncated preceding line), got payload={payload!r}",
               any(e.get("event") == "error" and isinstance(e.get("data"), dict)
                   and e["data"].get("type") == "error" for e in payload))

# ---- Group J: overflow table additions (M6) -------------------------------




@test
def test_overflow_databricks_wording_fixable_retry_succeeds(ctx: Ctx):
    """Real Databricks/Claude-Code-parsed wording is "... exceed context
    limit: A + B > L" (finding 3), sent inside the real Databricks error
    shape {"error_code","message"} with NO nested "error" object (finding 2).
    L=16384, A=6000 -> retry budget L-A-256=10128, comfortably above finding
    1's min(requested, 4096) floor, so this stays genuinely fixable."""
    ctx.require_dbx_bridge()
    def handler(h, body):
        max_tokens = (body or {}).get("max_tokens", 999999)
        if max_tokens <= 10128:
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"content": "overflow retry succeeded"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])
        else:
            send_json_response(h, 400, {
                "error_code": "BAD_REQUEST",
                "message": "Bad Request: input length and max_tokens exceed context limit: 6000 + 12000 > 16384",
            })
    ctx.dbx_mock.handlers["/serving-endpoints/databricks-overflow-case/invocations"] = handler
    body = areq("dbx:databricks-overflow-case", [{"role": "user", "content": "hi"}], max_tokens=12000)
    status, _, events, conn = ctx.dbx_call("/v1/messages", body)
    conn.close()
    ctx.check(f"overflow wording -> fixable retry -> 200, got {status}", status == 200)
    msg = assemble_message(events)
    ctx.check(f"text == 'overflow retry succeeded', got {text_of(msg)!r}", text_of(msg) == "overflow retry succeeded")



@test
def test_overflow_prompt_alone_exceeds_limit_no_retry_t_gt_l(ctx: Ctx):
    ctx.require_dbx_bridge()
    def handler(h, body):
        send_json_response(h, 400, {"error": {"message": "This model's maximum context length is 8192 tokens. However, you requested 9500 tokens (9000 in the messages, 500 in the completion)."}})
    ctx.dbx_mock.handlers["/serving-endpoints/databricks-overflow-unfixable/invocations"] = handler
    ctx.dbx_mock.clear()
    body = areq("dbx:databricks-overflow-unfixable", [{"role": "user", "content": "hi"}])
    status, _, payload, conn = ctx.dbx_call("/v1/messages", body)
    conn.close()
    ctx.check(f"prompt > limit -> final client status 400, got {status}", status == 400)
    err_msg = ((payload or {}).get("error") or {}).get("message", "") if isinstance(payload, dict) else ""
    m = re.match(r"^prompt is too long: (\d+) tokens > 8192 maximum$", err_msg)
    ctx.check(f"error message matches regex, got {err_msg!r}", m is not None)
    if m:
        token_count = int(m.group(1))
        ctx.check(f"captured token count > 8192, got {token_count}", token_count > 8192)
    ctx.check(f"only ONE request reached the mock (no retry), got {len(ctx.dbx_mock.requests)}", len(ctx.dbx_mock.requests) == 1)

# ---- Group K: --probe against mocks, models.json -> launcher env (M4/M6) -




@test
def test_probe_databricks_mock_lists_endpoints(ctx: Ctx):
    ctx.require_bridge_file_only()
    mock = GenericMockUpstream()
    try:
        mock.start()
        def handler(h, body):
            send_json_response(h, 200, {"endpoints": [{"name": "alpha"}, {"name": "beta"}]})
        mock.handlers["/api/2.0/serving-endpoints"] = handler
        env = dict(os.environ)
        env.update({
            "BRIDGE_DBX_BASE_URL": mock.base_url,
            "BRIDGE_DBX_TOKEN": "x",
            "BRIDGE_ENV_FILE": str(Path(tempfile.mkdtemp()) / "empty"),
            "BRIDGE_TEST_HOME": str(Path(tempfile.mkdtemp())),
            "BRIDGE_STATE_DIR": str(Path(tempfile.mkdtemp())),
        })
        env.pop("CLAUDECODE", None)
        proc = subprocess.run([sys.executable, str(BRIDGE_PY), "--probe"],
                               env=env, capture_output=True, text=True, timeout=30, cwd=str(REPO_DIR))
        ctx.check(f"probe exit code 0, got {proc.returncode}", proc.returncode == 0)
        ctx.check(f"stdout contains 'alpha' and 'beta', got {proc.stdout[-500:]!r}",
                   "alpha" in proc.stdout and "beta" in proc.stdout)
    finally:
        mock.stop()



@test
def test_probe_databricks_mock_403_pass_names_explicitly(ctx: Ctx):
    ctx.require_bridge_file_only()
    mock = GenericMockUpstream()
    try:
        mock.start()
        def handler(h, body):
            send_json_response(h, 403, {"error": "forbidden"})
        mock.handlers["/api/2.0/serving-endpoints"] = handler
        env = dict(os.environ)
        env.update({
            "BRIDGE_DBX_BASE_URL": mock.base_url,
            "BRIDGE_DBX_TOKEN": "x",
            "BRIDGE_ENV_FILE": str(Path(tempfile.mkdtemp()) / "empty"),
            "BRIDGE_TEST_HOME": str(Path(tempfile.mkdtemp())),
            "BRIDGE_STATE_DIR": str(Path(tempfile.mkdtemp())),
        })
        env.pop("CLAUDECODE", None)
        proc = subprocess.run([sys.executable, str(BRIDGE_PY), "--probe"],
                               env=env, capture_output=True, text=True, timeout=30, cwd=str(REPO_DIR))
        ctx.check(f"probe exit 0, got {proc.returncode}", proc.returncode == 0)
        ctx.check(f"stdout contains exact token-can-run substring, got {proc.stdout[-500:]!r}",
                   "token can run inference but not list endpoints -- pass names explicitly" in proc.stdout)
    finally:
        mock.stop()



@test
def test_probe_databricks_unreachable_host(ctx: Ctx):
    ctx.require_bridge_file_only()
    env = dict(os.environ)
    env.update({
        "BRIDGE_DBX_BASE_URL": "https://bridge-test-does-not-exist.invalid",
        "BRIDGE_DBX_TOKEN": "x",
        "BRIDGE_ENV_FILE": str(Path(tempfile.mkdtemp()) / "empty"),
        "BRIDGE_TEST_HOME": str(Path(tempfile.mkdtemp())),
        "BRIDGE_STATE_DIR": str(Path(tempfile.mkdtemp())),
    })
    env.pop("CLAUDECODE", None)
    proc = subprocess.run([sys.executable, str(BRIDGE_PY), "--probe"],
                           env=env, capture_output=True, text=True, timeout=30, cwd=str(REPO_DIR))
    ctx.check(f"probe exit 0, got {proc.returncode}", proc.returncode == 0)
    ctx.check(f"stdout contains 'unreachable' and 'VPN', got {proc.stdout[-500:]!r}",
               "unreachable" in proc.stdout and "VPN" in proc.stdout)



@test
def test_probe_openrouter_mock_caches_models_json(ctx: Ctx):
    ctx.require_bridge_file_only()
    mock = GenericMockUpstream()
    try:
        mock.start()
        def handler(h, body):
            send_json_response(h, 200, {"data": [
                {"id": "vendor/model-a", "context_length": 32000, "top_provider": {"max_completion_tokens": 4096}},
                {"id": "vendor/model-b", "context_length": 128000, "top_provider": {"max_completion_tokens": 8192}},
            ]})
        mock.handlers["/models"] = handler
        state_dir = Path(tempfile.mkdtemp())
        env = dict(os.environ)
        env.update({
            "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
            "OPENROUTER_API_KEY": "test-key",
            "BRIDGE_ENV_FILE": str(Path(tempfile.mkdtemp()) / "empty"),
            "BRIDGE_TEST_HOME": str(Path(tempfile.mkdtemp())),
            "BRIDGE_STATE_DIR": str(state_dir),
        })
        env.pop("CLAUDECODE", None)
        proc = subprocess.run([sys.executable, str(BRIDGE_PY), "--probe"],
                               env=env, capture_output=True, text=True, timeout=30, cwd=str(REPO_DIR))
        ctx.check(f"probe exit 0, got {proc.returncode}", proc.returncode == 0)
        models_path = state_dir / "models.json"
        ctx.check(f"models.json exists at {models_path}", models_path.exists())
        if models_path.exists():
            models = json.loads(models_path.read_text(encoding="utf-8"))
            ctx.check(f"vendor/model-a present, keys {list(models.keys())!r}", "vendor/model-a" in models)
            ctx.check(f"vendor/model-a.context_length == 32000, got {models.get('vendor/model-a', {}).get('context_length')!r}",
                       models.get("vendor/model-a", {}).get("context_length") == 32000)
            ctx.check(f"vendor/model-a.max_output_tokens == 4096, got {models.get('vendor/model-a', {}).get('max_output_tokens')!r}",
                       models.get("vendor/model-a", {}).get("max_output_tokens") == 4096)
    finally:
        mock.stop()



@test
def test_models_json_feeds_launcher_context_env(ctx: Ctx):
    ctx.require_bridge_file_only()
    lc = LauncherCtx()
    try:
        models_json = lc.state_dir / "models.json"
        models_json.parent.mkdir(parents=True, exist_ok=True)
        models_json.write_text(json.dumps({
            "mock/model": {"context_length": 55555, "max_output_tokens": 7777},
        }), encoding="utf-8")
        proc = lc.run_launch(model="or:mock/model", claude_args=["-p", "hi"], exit_code=7)
        ctx.check(f"launch exit 7, got {proc.returncode}", proc.returncode == 7)
        dump = lc.read_dump()
        settings_env = (dump.get("settings_content") or {}).get("env") or {}
        ctx.check(f"CLAUDE_CODE_MAX_CONTEXT_TOKENS == '55555', got {settings_env.get('CLAUDE_CODE_MAX_CONTEXT_TOKENS')!r}",
                   settings_env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") == "55555")
        ctx.check(f"CLAUDE_CODE_MAX_OUTPUT_TOKENS == '7777', got {settings_env.get('CLAUDE_CODE_MAX_OUTPUT_TOKENS')!r}",
                   settings_env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == "7777")
    finally:
        lc.stop()


# ---- Group L: launcher stale-hash restart, BRIDGE_DUMP files (M6) ---------


@test
def test_launcher_stale_hash_restart(ctx: Ctx):
    """Design review section 2 'Server lifecycle': if a server from an OLDER
    bridge.py (different sha1) already holds the launch port, `launch` must
    shut it down (POST /shutdown with the token already on disk, since the
    stale server wrote it to the SAME state dir), wait for the port to free,
    then spawn a fresh one -- rather than failing to bind or silently
    reusing the stale server. Hermetic: builds a byte-mutated copy of
    bridge.py (different sha1, same behavior) and runs THAT directly as
    `--serve` first."""
    ctx.require_bridge_file_only()
    lc = LauncherCtx()
    stale_proc = None
    stale_log = None
    try:
        real_bytes = BRIDGE_PY.read_bytes()
        mutated_bytes = real_bytes + b"\n# mutated-for-stale-hash-test\n"
        mutated_path = lc.tmp_root / "bridge_mutated.py"
        mutated_path.write_bytes(mutated_bytes)

        stale_log = open(lc.tmp_root / "stale-stdio.log", "wb")
        stale_proc = subprocess.Popen(
            [sys.executable, str(mutated_path), "--serve", "--port", str(lc.port), "--state-dir", str(lc.state_dir)],
            stdout=stale_log, stderr=subprocess.STDOUT, cwd=str(REPO_DIR),
        )

        stale_hash = None
        stale_pid = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                c = http.client.HTTPConnection("127.0.0.1", lc.port, timeout=1)
                c.request("GET", "/healthz")
                r = c.getresponse()
                data = json.loads(r.read())
                c.close()
                if r.status == 200 and data.get("service") == "claude-bridge":
                    stale_hash, stale_pid = data.get("hash"), data.get("pid")
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        ctx.check(f"the mutated stale server became healthy with a hash, got hash={stale_hash!r} pid={stale_pid!r}",
                   bool(stale_hash) and isinstance(stale_pid, int))

        real_hash = hashlib.sha1(real_bytes).hexdigest()
        ctx.check(f"stale server's hash differs from the real bridge.py's own hash, stale={stale_hash!r} real={real_hash!r}",
                   stale_hash != real_hash)

        proc = lc.run_launch(model="or:mock/model", claude_args=["-p", "hi"], exit_code=7, timeout=30.0)
        ctx.check(f"launch against a stale-hash server still propagates the fake claude's exit code 7, got {proc.returncode} "
                   f"(stderr tail: {proc.stderr[-1000:] if proc.stderr else ''!r})",
                   proc.returncode == 7)

        info = lc.server_json()
        new_pid = info.get("pid") if isinstance(info, dict) else None
        ctx.check(f"server.json reports a NEW pid different from the stale one ({stale_pid!r}), got {new_pid!r}",
                   isinstance(new_pid, int) and new_pid != stale_pid)

        c = http.client.HTTPConnection("127.0.0.1", lc.port, timeout=5)
        c.request("GET", "/healthz")
        r = c.getresponse()
        data = json.loads(r.read())
        c.close()
        ctx.check(f"/healthz now reports the REAL bridge.py's hash, got {data.get('hash')!r} expected {real_hash!r}",
                   data.get("hash") == real_hash)
        ctx.check(f"/healthz pid matches the new server.json pid, got {data.get('pid')!r} vs {new_pid!r}",
                   data.get("pid") == new_pid)
    finally:
        if stale_proc is not None and stale_proc.poll() is None:
            try:
                stale_proc.kill()
                stale_proc.wait(timeout=5)
            except Exception:
                pass
        if stale_log is not None:
            try:
                stale_log.close()
            except Exception:
                pass
        lc.stop()


@test
def test_launcher_foreign_port_fails_fast(ctx: Ctx):
    """A port answering with a 200 that is NOT claude-bridge's own /healthz
    shape must fail launch fast with a plain message naming the port,
    rather than attempting to spawn (which would just bind-fail loudly and
    confusingly) or silently treating it as usable."""
    ctx.require_bridge_file_only()
    lc = LauncherCtx()
    foreign = None
    try:
        class _ForeignHandler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"service": "not-claude-bridge", "ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        foreign = HTTPServer(("127.0.0.1", lc.port), _ForeignHandler)
        foreign_thread = threading.Thread(target=foreign.serve_forever, daemon=True)
        foreign_thread.start()

        t0 = time.monotonic()
        proc = lc.run_launch(model="or:mock/model", claude_args=["-p", "hi"], exit_code=7, timeout=20.0)
        elapsed = time.monotonic() - t0
        ctx.check(f"launch against a foreign (non-claude-bridge) port exits non-zero, got {proc.returncode}",
                   proc.returncode != 0)
        # finding 12: without this, a regression that instead fell through to
        # the 10s spawn-and-poll-healthz timeout path would ALSO eventually
        # fail with the port number showing up somewhere in stderr, silently
        # passing the check below despite being 3-4x slower than the real
        # "foreign -> fail immediately" contract.
        ctx.check(f"fails fast (< 3s), not via the 10s spawn/poll timeout path, got {elapsed:.2f}s", elapsed < 3.0)
        stderr = proc.stderr or ""
        ctx.check(f"stderr names the port {lc.port} plainly, got {stderr[-800:]!r}", str(lc.port) in stderr)
    finally:
        if foreign is not None:
            try:
                foreign.shutdown()
                foreign.server_close()
            except Exception:
                pass
        lc.stop()


@test
def test_stop_waits_for_port_to_free(ctx: Ctx):
    """finding 13: `--stop` must not print success until the port has
    actually stopped accepting connections -- POST /shutdown answers 200
    immediately but the real server.shutdown() only fires ~200ms later from
    a background timer, so an immediate `launch` right after used to see the
    dying server still answering /healthz and reuse it, handing its child
    `claude` an ECONNREFUSED mid-request."""
    ctx.require_bridge_file_only()
    bridge = BridgeProc(ctx.mock)
    bridge.start()
    port = bridge.port
    state_dir = bridge.state_dir
    try:
        env = dict(os.environ)
        env.update({"BRIDGE_STATE_DIR": str(state_dir)})
        env.pop("CLAUDECODE", None)
        result = subprocess.run([sys.executable, str(BRIDGE_PY), "--stop"],
                                 env=env, capture_output=True, text=True, timeout=15, cwd=str(REPO_DIR))
        ctx.check(f"--stop exits 0, got {result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}",
                   result.returncode == 0)
        ctx.check(f"port {port} is already free the instant --stop returns, got busy={not _port_is_free(port)}",
                   _port_is_free(port))
    finally:
        bridge.stop()


@test
def test_bridge_dump_writes_all_four_kinds(ctx: Ctx):
    """M6: BRIDGE_DUMP=1 must write claude-request, upstream-request,
    upstream-stream, and emitted-events dumps per /v1/messages request,
    next to each other under <state_dir>/dumps/."""
    ctx.require_bridge_file_only()
    bridge = BridgeProc(ctx.mock, extra_env={"BRIDGE_DUMP": "1"})
    try:
        bridge.start()
        body = areq("or:mock/model", [{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
        status, _, events, conn = http_call(bridge.port, "POST", "/v1/messages", body=body,
                                             headers={"x-api-key": bridge.token})
        conn.close()
        ctx.check(f"dump-enabled request -> 200, got {status}", status == 200)
        validate_anthropic_stream(events)

        dumps_dir = bridge.state_dir / "dumps"
        files = []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if dumps_dir.exists():
                files = list(dumps_dir.iterdir())
                if files:
                    break
            time.sleep(0.1)

        found_kinds = set()
        for f in files:
            name = f.name
            if name.endswith(".json") and "-" in name:
                found_kinds.add(name.split("-", 1)[1][: -len(".json")])
        for expected in ("claude-request", "upstream-request", "upstream-stream", "emitted-events"):
            ctx.check(f"dump kind {expected!r} present under {dumps_dir}, got kinds {found_kinds!r}",
                       expected in found_kinds)
    finally:
        bridge.stop()


# ---- Group M: in-message role:system hoisting (root-cause fix) -----------
#
# Claude Code 2.1.280 sends a trailing `{"role": "system", ...}` message
# INSIDE `messages`, after the user turn (a "# Environment ..." block).
# Forwarding that in place as a trailing `role: system` OpenAI message made
# some upstreams treat it as the last word and continue writing environment
# text instead of answering. Every in-message system entry must be hoisted
# into the one leading system message instead.


@test
def test_trailing_inmessage_system_hoisted(ctx: Ctx):
    """Synthetic version of the real shape: top-level system, a user turn
    with 3 text parts (two system-reminders + the prompt), then a trailing
    in-message `role: system` turn. It must be hoisted, not forwarded."""
    ctx.require_bridge()
    system = "TOP-LEVEL SYS"
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>reminder one</system-reminder>"},
            {"type": "text", "text": "<system-reminder>reminder two</system-reminder>"},
            {"type": "text", "text": "reply with the single word pong"},
        ]},
        {"role": "system", "content": [
            {"type": "text", "text": "# Environment\nsome environment info"},
        ]},
    ]
    body = areq("or:mock/model", messages, system=system)
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"trailing in-message system -> 200, got {status}", status == 200)
    up_messages = ctx.mock.requests[-1]["body"]["messages"]

    sys_indices = [i for i, m in enumerate(up_messages) if m["role"] == "system"]
    ctx.check(f"exactly one system message, at index 0, got indices {sys_indices!r}", sys_indices == [0])
    ctx.check(f"hoisted content is top-level text then in-message text joined by a blank line, got {up_messages[0]['content']!r}",
               up_messages[0]["content"] == "TOP-LEVEL SYS\n\n# Environment\nsome environment info")
    ctx.check(f"no role:system anywhere after index 0, got {[m['role'] for m in up_messages]!r}",
               all(m["role"] != "system" for m in up_messages[1:]))
    ctx.check(f"last message is the user turn, got {up_messages[-1]!r}", up_messages[-1]["role"] == "user")
    last_text = extract_text(up_messages[-1]["content"])
    ctx.check(f"the user turn's content ends with the prompt, got {last_text!r}",
               last_text.rstrip().endswith("reply with the single word pong"))


@test
def test_midhistory_system_hoisted_tool_pairing_preserved(ctx: Ctx):
    """A `role: system` message sitting between an assistant tool_use turn
    and the user turn carrying its tool_result (assistant -> system -> user)
    must be hoisted out without disturbing that pairing, and without
    disturbing user/assistant turn order."""
    ctx.require_bridge()
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "please read the file"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_mid", "name": "Read", "input": {"file_path": "f.txt"}},
        ]},
        {"role": "system", "content": [
            {"type": "text", "text": "# Environment\nmid-history env info"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_mid", "content": "file contents"},
        ]},
    ]
    body = areq("or:mock/model", messages, system="TOP")
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"mid-history system message -> 200, got {status}", status == 200)
    up_messages = ctx.mock.requests[-1]["body"]["messages"]
    roles = [m["role"] for m in up_messages]

    ctx.check(f"exactly one system message, hoisted to index 0, got {roles!r}",
               roles.count("system") == 1 and roles[0] == "system")
    ctx.check(f"hoisted text is top-level + mid-history text joined by a blank line, got {up_messages[0]['content']!r}",
               up_messages[0]["content"] == "TOP\n\n# Environment\nmid-history env info")
    ctx.check(f"user/assistant/tool order preserved around the removed system turn, got {roles!r}",
               roles == ["system", "user", "assistant", "tool"])

    asst = next(m for m in up_messages if m["role"] == "assistant")
    ctx.check(f"assistant tool_use turn intact (content null, one tool_call), got {asst!r}",
               asst.get("content") is None and len(asst.get("tool_calls") or []) == 1
               and asst["tool_calls"][0]["id"] == "toolu_mid")

    tool_msgs = [m for m in up_messages if m["role"] == "tool"]
    ctx.check(f"exactly one tool-result message, still paired to toolu_mid, got {tool_msgs!r}",
               len(tool_msgs) == 1 and tool_msgs[0].get("tool_call_id") == "toolu_mid"
               and tool_msgs[0].get("content") == "file contents")


@test
def test_inmessage_system_creates_leading_system_with_no_top_level(ctx: Ctx):
    """No top-level `system` field at all, only an in-message one -- a
    leading system message must still be created."""
    ctx.require_bridge()
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "system", "content": [
            {"type": "text", "text": "# Environment\nonly source of system text"},
        ]},
    ]
    body = areq("or:mock/model", messages)  # no system= kwarg -> no top-level "system" key at all
    ctx.check("sanity: request body carries no top-level 'system' key", "system" not in body)
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"in-message-only system -> 200, got {status}", status == 200)
    up_messages = ctx.mock.requests[-1]["body"]["messages"]
    ctx.check(f"a leading system message was created, got {up_messages!r}",
               len(up_messages) >= 1 and up_messages[0]["role"] == "system")
    ctx.check(f"its content is exactly the in-message text, got {up_messages[0]['content']!r}",
               up_messages[0]["content"] == "# Environment\nonly source of system text")
    ctx.check(f"no other system message present, got {[m['role'] for m in up_messages]!r}",
               [m["role"] for m in up_messages].count("system") == 1)


@test
def test_all_text_parts_collapse_image_list_stays(ctx: Ctx):
    """After the final merge pass: a content list where every part is
    {"type":"text"} collapses to one string joined by "\\n\\n"; a list
    containing an image part is left as a list."""
    ctx.require_bridge()
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "part A"},
            {"type": "text", "text": "part B"},
        ]},
    ]
    body = areq("or:mock/model", messages)
    status, _, _, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"all-text-parts request -> 200, got {status}", status == 200)
    user_msgs = [m for m in ctx.mock.requests[-1]["body"]["messages"] if m["role"] == "user"]
    ctx.check(f"exactly one user message, got {len(user_msgs)}", len(user_msgs) == 1)
    ctx.check(f"an all-text content list collapses to one string joined by a blank line, got {user_msgs[0]['content']!r}",
               user_msgs[0]["content"] == "part A\n\npart B")

    messages2 = [
        {"role": "user", "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
        ]},
    ]
    body2 = areq("or:mock/model", messages2)
    status2, _, _, conn2 = ctx.call("/v1/messages", body2)
    conn2.close()
    ctx.check(f"text+image request -> 200, got {status2}", status2 == 200)
    user_msgs2 = [m for m in ctx.mock.requests[-1]["body"]["messages"] if m["role"] == "user"]
    ctx.check(f"exactly one user message, got {len(user_msgs2)}", len(user_msgs2) == 1)
    content2 = user_msgs2[0]["content"]
    ctx.check(f"a content list containing an image part stays a list, got {content2!r}", isinstance(content2, list))
    has_image = isinstance(content2, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in content2)
    ctx.check(f"the image_url part is present in that list, got {content2!r}", has_image)


@test
def test_end_to_end_no_system_after_user_turn(ctx: Ctx):
    """End-to-end through the real mock upstream, reproducing the actual
    Claude Code 2.1.280 shape: a 3-part top-level system (incl. a billing
    header block), a user turn with 3 text parts (two system-reminders + the
    prompt), then a trailing in-message system turn. The recorded body's
    last message must be the user turn, with no system message after it."""
    ctx.require_bridge()
    system = [
        {"type": "text", "text": "You are Claude Code, Anthropic's CLI."},
        {"type": "text", "text": "x-anthropic-billing-header: acct-999"},
        {"type": "text", "text": "Be concise."},
    ]
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>reminder one</system-reminder>"},
            {"type": "text", "text": "<system-reminder>reminder two</system-reminder>"},
            {"type": "text", "text": "reply with the single word pong"},
        ]},
        {"role": "system", "content": [
            {"type": "text", "text": "# Environment\nPrimary working directory: /repo\nToday's date is 2026-09-23."},
        ]},
    ]
    body = areq("or:mock/model", messages, system=system)
    status, _, payload, conn = ctx.call("/v1/messages", body)
    conn.close()
    ctx.check(f"end-to-end request -> 200, got {status}", status == 200)
    validate_anthropic_stream(payload)
    msg = assemble_message(payload)
    ctx.check(f"mock's canned reply still comes through untouched, got {text_of(msg)!r}", text_of(msg) == "pong")

    up_messages = ctx.mock.requests[-1]["body"]["messages"]
    ctx.check(f"last recorded message is the user turn, got {up_messages[-1]!r}", up_messages[-1]["role"] == "user")
    ctx.check(f"no system message anywhere after index 0, got {[m['role'] for m in up_messages]!r}",
               all(m["role"] != "system" for m in up_messages[1:]))
    sys_text = up_messages[0]["content"]
    ctx.check(f"billing-header block still dropped from the hoisted system text, got {sys_text!r}",
               "x-anthropic-billing-header" not in sys_text)
    ctx.check(f"top-level system text present, got {sys_text!r}", "Claude Code" in sys_text and "Be concise" in sys_text)
    ctx.check(f"in-message environment text appended after it, got {sys_text!r}", "# Environment" in sys_text)
    ctx.check(f"top-level text precedes the environment text, got {sys_text!r}",
               sys_text.index("Be concise") < sys_text.index("# Environment"))


# TESTS_END_MARKER -- new @test functions are inserted directly above this.


DBX_MOCK_TOKEN = "test-dbx-token"


def main() -> int:
    ctx = Ctx()
    if BRIDGE_EXISTS:
        try:
            ctx.mock = MockUpstream().start()
            ctx.bridge = BridgeProc(ctx.mock).start()
        except Exception as e:
            ctx.bridge_start_error = f"{type(e).__name__}: {e}"
            ctx.bridge = None
    else:
        ctx.bridge_start_error = "bridge.py not found"

    if BRIDGE_EXISTS:
        try:
            ctx.dbx_mock = GenericMockUpstream().start()
            ctx.dbx_bridge = BridgeProc(ctx.mock, extra_env={
                "BRIDGE_DBX_BASE_URL": ctx.dbx_mock.base_url,
                "BRIDGE_DBX_TOKEN": DBX_MOCK_TOKEN,
            }).start()
        except Exception as e:
            ctx.dbx_bridge_start_error = f"{type(e).__name__}: {e}"
            ctx.dbx_bridge = None
    else:
        ctx.dbx_bridge_start_error = "bridge.py not found"

    try:
        results, passed, failed, skipped = run_all(ctx)
    finally:
        if ctx.bridge is not None:
            try:
                ctx.bridge.stop()
            except Exception:
                pass
        if ctx.mock is not None:
            try:
                ctx.mock.stop()
            except Exception:
                pass
        if ctx.dbx_bridge is not None:
            try:
                ctx.dbx_bridge.stop()
            except Exception:
                pass
        if ctx.dbx_mock is not None:
            try:
                ctx.dbx_mock.stop()
            except Exception:
                pass

    for name, status, detail, dt in results:
        if status == "PASS":
            print(f"[PASS] {name} ({dt:.2f}s)")
        elif status == "SKIP":
            print(f"[SKIP] {name}: {detail}")
        else:
            print(f"[FAIL] {name}: {detail}")

    total = len(results)
    print("-" * 72)
    print(f"RESULT: {total} tests, {ctx.checks} checks -- {passed} passed, {failed} failed, {skipped} skipped")
    if not BRIDGE_EXISTS:
        print(f"NOTE: bridge.py not found at {BRIDGE_PY} -- all bridge-dependent tests were skipped, not failed.")
    elif ctx.bridge is None:
        print(f"NOTE: bridge.py exists but the shared server fixture could not start: {ctx.bridge_start_error}")

    exit_code = 1 if (failed > 0 or passed == 0) else 0
    print(f"exit={exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

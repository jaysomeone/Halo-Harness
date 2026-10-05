"""tests.helpers.mock_openai -- mock OpenAI-dialect upstream server, COPIED
from test_bridge.py's own "Section 1: Mock OpenAI-dialect upstream" and
"Section 2" event-list helpers (MockUpstream, the SCENARIOS table, the
_finish/send_json_response/SSE-framing helpers, validate_anthropic_stream,
assemble_message) -- byte-identical to test_bridge.py's own copy, which is
NOT changed to import this (per the H0 brief: "do not refactor test_bridge.py
to import it"). Used by this project's OWN test suite (tests/test_stream.py,
tests/test_loop_headless.py, ...) so it can run entirely independently of
the proxy's test file.

Two additions beyond the straight copy, both at the bottom of this file:
  * `events_from_stream_completion(gen)` -- wraps providers.stream.
    stream_completion's raw Anthropic-event dicts into the {"event":...,
    "data":...} shape validate_anthropic_stream/assemble_message expect
    (that shape comes from test_bridge.py's own SSE-line decoder, which
    this suite doesn't need since it calls stream_completion as a plain
    Python generator, never over HTTP).
  * `ScriptedTurns` -- a stateful scenario keyed on the incoming request's
    message history (specifically, whether the last message has role
    "tool"), for scripting a multi-turn tool-call loop. Not used by any H0
    test (H0 has no tools) -- included now per the brief for H1 to build on.
"""

from __future__ import annotations

import json
import re
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

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


def _scn_error_then_keeps_streaming(h, body):
    """finding 12: an upstream that sends a mid-stream error chunk and then
    KEEPS SENDING more data afterward (verified: 30 more chunks over ~3s) --
    proves the client actually shuts the socket instead of just stopping
    its own read loop while the connection dangles open."""
    start_sse(h)
    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    write_sse_chunk(h, None, {"error": {"message": "upstream exploded", "type": "server_error"}})
    for _ in range(30):
        write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"content": "x"}}]})
        time.sleep(0.1)
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


def _scn_doctor(good: bool):
    """Halo 2.0.3 round 5f: the openai-chat-dialect twin of `tests.
    test_doctor_local_5d`'s own `_doctor_scenario` (same content-based
    dispatch -- which of `doctor_local.py`'s four fixed-order steps this
    request is, inferred from the system/user text and whether `tools`
    is non-empty, since there is no per-step model name to key on here).
    `good=False` makes each step's own reply fail validation instead of
    the request itself erroring, exactly like the ndjson twin does."""
    def responder(h, body):
        messages = body.get("messages") or []
        text_all = " ".join(str(m.get("content") or "") for m in messages)
        tools = body.get("tools") or []
        if tools:
            args = ({"file_path": "fixture.txt"} if good else {"not_file_path": 1})
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "call_1", "type": "function",
                     "function": {"name": "Read", "arguments": json.dumps(args)}}]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ])
        elif "Reply with the single word ready" in text_all:
            _finish(h, [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                        {"choices": [{"index": 0, "delta": {"content": "ready"}}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}])
        elif "Reply with exactly this JSON object" in text_all:
            content = json.dumps({"answer": "halo"}) if good else "sure, the answer is halo"
            _finish(h, [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                        {"choices": [{"index": 0, "delta": {"content": content}}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}])
        elif "Summarize the following conversation" in text_all:
            content = "Two debug lines were added and all tests passed." if good else ""
            _finish(h, [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                        {"choices": [{"index": 0, "delta": {"content": content}}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}])
        else:
            _finish(h, [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                        {"choices": [{"index": 0, "delta": {"content": "ok"}}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}])
    return responder


SCENARIOS = {
    "model": _scn_model,
    "doctor-good": _scn_doctor(True),
    "doctor-bad": _scn_doctor(False),
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
    "error-then-keeps-streaming": _scn_error_then_keeps_streaming,
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

        # Halo 2.0.3 round 4: path/bearer validation, both opt-in via the
        # constructor (see MockUpstream's own docstring) -- a plain
        # `MockUpstream()` (every pre-round-4 call site) gets `path_prefix
        # == "/api/v1"` and `expected_bearer is None`, so `expected_path`
        # always matches (the request was built from THIS SAME base_url)
        # and the bearer check is skipped entirely: byte-for-byte the old
        # behavior.
        expected_path = mock.path_prefix.rstrip("/") + "/chat/completions"
        if self.path != expected_path:
            send_json_response(self, 404, {"error": f"path {self.path!r} not served (expected {expected_path!r})"})
            return
        if mock.expected_bearer is not None:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {mock.expected_bearer}":
                send_json_response(self, 401, {"error": {"message": "invalid bearer token for this mock",
                                                           "type": "authentication_error"}})
                return

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
        # Halo 2.0.3 round 4: `GET <path_prefix>/models` (the Hugging Face
        # router catalog fetch, and -- same shape -- OpenRouter's own
        # `/models`) -- served only once a test sets `mock.models_
        # response`; every pre-round-4 caller never sets it, so this falls
        # straight through to the original unconditional 404 below.
        mock: MockUpstream = self.server.mock  # type: ignore[attr-defined]
        expected_path = mock.path_prefix.rstrip("/") + "/models"
        if self.path == expected_path and mock.models_response is not None:
            with mock._lock:
                mock._requests.append({
                    "method": "GET", "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()}, "body": None,
                    "ts": time.monotonic(),
                })
            if mock.expected_bearer is not None:
                auth = self.headers.get("Authorization", "")
                if auth != f"Bearer {mock.expected_bearer}":
                    send_json_response(self, 401, {"error": {"message": "invalid bearer token for this mock",
                                                               "type": "authentication_error"}})
                    return
            send_json_response(self, 200, mock.models_response)
            return
        send_json_response(self, 404, {"error": "mock upstream only serves POST"})


class MockUpstream:
    """Halo 2.0.3 round 4: two constructor options, both optional and
    both defaulting to the pre-round-4 behavior exactly, so every existing
    `MockUpstream()` call site (OpenRouter/Databricks-chat tests) is
    unaffected --

    * `path_prefix` (default `"/api/v1"`, this class's own long-standing
      default): `base_url` serves from `http://127.0.0.1:<port><path_
      prefix>`, and `do_POST`/`do_GET` now VALIDATE the incoming request's
      path against `<path_prefix>/chat/completions` / `<path_prefix>/
      models` -- a real base_url/path-building bug would now 404 here
      instead of silently working because the old handler never checked
      the path at all. Lets the SAME scripted `SCENARIOS` serve as the
      Hugging Face router stand-in (`/v1`) and the dedicated-endpoint
      stand-in (an arbitrary prefix), per the round 4 brief's "parameterize
      ... rather than writing a second fake."
    * `expected_bearer` (default `None`, meaning "don't check" -- the
      pre-round-4 behavior): when set, a request whose `Authorization`
      header isn't exactly `f"Bearer {expected_bearer}"` gets a 401 instead
      of running its scenario, for the "bearer from HF_TOKEN vs an endpoint
      token, never cross-wired" pinning tests -- two `MockUpstream`
      instances with two different `expected_bearer` values, standing in
      for the router and a dedicated endpoint at once.

    `models_response`, set after construction (never a constructor arg --
    most callers never need it), is served verbatim on `GET <path_prefix>/
    models` once set; `None` (the default) keeps `do_GET`'s original
    unconditional 404."""

    def __init__(self, *, path_prefix: str = "/api/v1", expected_bearer: "str | None" = None):
        self._server: _ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._requests: list[dict] = []
        self._lock = threading.Lock()
        self.disconnect_events: list[dict] = []
        self.path_prefix = path_prefix
        self.models_response = None
        self.expected_bearer = expected_bearer

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.path_prefix}"

    @property
    def requests(self) -> list[dict]:
        return self._requests

    def clear(self) -> None:
        with self._lock:
            self._requests.clear()
            self.disconnect_events.clear()

    def start(self, port: int = 0) -> "MockUpstream":
        """`port=0` (default) picks a free OS-assigned port; a caller that
        needs to bind a SPECIFIC port (H0 #14 case 6: connect to a port
        with nothing listening yet, then bring the mock up on that exact
        port to prove the connect-retry) passes it explicitly."""
        self._server = _ThreadingHTTPServer(("127.0.0.1", port), _Handler)
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



# ---- H1 scaffold: a stateful multi-turn scenario ---------------------------

class ScriptedTurns:
    """A scenario callable (`fn(handler, body)`) that scripts a sequence of
    upstream replies keyed on how many `role: "tool"` messages have already
    appeared in the request's `messages` -- i.e. how many tool-call round
    trips have completed so far. `steps` is a list of either:
      * a list of raw OpenAI-chat SSE chunk dicts (passed to `_finish` as-is), or
      * a callable `(handler, body) -> None` for a step needing custom
        behavior (a non-2xx response, a deliberate disconnect, ...).
    Register an instance directly in a SCENARIOS-like dict: `{"my-scn": ScriptedTurns([...])}`
    -- MockUpstream's dispatcher only requires the value be callable.
    Not used by any H0 test (H0 sends no `tools`, so no reply can ever
    contain a `role: "tool"` message) -- provided for H1's tool-loop tests.
    """

    def __init__(self, steps: list):
        self.steps = steps

    def __call__(self, handler, body) -> None:
        messages = (body or {}).get("messages") or []
        step_index = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool")
        step_index = min(step_index, len(self.steps) - 1) if self.steps else 0
        if not self.steps:
            send_json_response(handler, 500, {"error": "ScriptedTurns has no steps configured"})
            return
        step = self.steps[step_index]
        if callable(step) and not isinstance(step, list):
            step(handler, body)
        else:
            _finish(handler, step)


def events_from_stream_completion(gen) -> list:
    """Consume a providers.stream.stream_completion(...) generator and wrap
    each raw Anthropic-shaped event dict into the {"event": <type>, "data":
    <event>} shape validate_anthropic_stream/assemble_message (copied above)
    expect -- that shape mirrors what an SSE line decoder would produce; a
    direct generator consumer doesn't have SSE framing to decode, but the
    event NAME always equals `data["type"]` for this dialect's own SSE
    output (bridge.py's own sse_frame() guarantees that), so reconstructing
    it here is exact, not an approximation."""
    wrapped = []
    for ev in gen:
        wrapped.append({"event": ev.get("type"), "data": ev})
    return wrapped

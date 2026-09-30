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


def _sse_anthropic_event(handler, event: dict) -> None:
    frame = f"event: {event['type']}\r\ndata: {json.dumps(event, ensure_ascii=False)}\r\n\r\n".encode("utf-8")
    _write(handler, b"%x\r\n" % len(frame) + frame + b"\r\n")


def _finish_anthropic(handler, body: dict) -> None:
    """H14 scope H: a minimal, real native-Anthropic-Messages SSE reply
    (message_start/content_block_start/_delta/_stop/message_delta/
    message_stop) -- just enough for `stream_anthropic_completion` to
    decode a real "pong"-shaped turn through the SAME mock server the
    openai-chat scenarios above use, so one mock covers both dialects. This
    is the DEFAULT/"ok" scenario on the anthropic gateway -- unchanged since
    H14, so every existing caller with no matching suffix below keeps
    getting byte-identical output."""
    _start_sse(handler)
    for ev in (
        {"type": "message_start", "message": {"id": "msg_mock", "type": "message", "role": "assistant",
         "content": [], "model": body.get("model", "claude"),
         "usage": {"input_tokens": 5, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "pong"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ):
        _sse_anthropic_event(handler, ev)
    _end_sse(handler)


# ---------------------------------------------------------------------------
# V2a: scenario dispatch for the native anthropic/v1/messages gateway (Claude
# foundation default; GLM/Kimi opt-in via `@anthropic`/`databricks.gateway.
# <endpoint>`) -- thinking blocks + signatures, tool_use (including Kimi's
# own verbatim `functions.<name>:<idx>` id), and the same error shapes as the
# openai-chat dialect above, keyed the SAME way (longest suffix of `model`).
# ---------------------------------------------------------------------------

def _usage_a(input_tokens=5, output_tokens=0, cache_read=0, cache_write=0) -> dict:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write}


def _ascn_thinking_and_signature(handler, body):
    """Thinking block + signature, then a plain text reply -- the "with
    thinking" half of "GLM/Kimi thinking shapes through the gateway (test
    both with and without thinking)"; family-agnostic (Claude/GLM/Kimi all
    decode identically -- AnthropicSSEDecoder never inspects `model`)."""
    _start_sse(handler)
    for ev in (
        {"type": "message_start", "message": {"id": "msg_think", "type": "message", "role": "assistant",
         "content": [], "model": body.get("model", "claude"), "usage": _usage_a()}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "let me consider this"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig_abc123"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "pong"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": _usage_a(input_tokens=500, output_tokens=30, cache_read=200, cache_write=50)},
        {"type": "message_stop"},
    ):
        _sse_anthropic_event(handler, ev)
    _end_sse(handler)


def _ascn_tool_use(handler, body):
    _start_sse(handler)
    for ev in (
        {"type": "message_start", "message": {"id": "msg_tool", "type": "message", "role": "assistant",
         "content": [], "model": body.get("model", "claude"), "usage": _usage_a()}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "toolu_01", "name": "Read", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"file_path"'}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": ':"pong.txt"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": _usage_a()},
        {"type": "message_stop"},
    ):
        _sse_anthropic_event(handler, ev)
    _end_sse(handler)


def _ascn_kimi_tool_id(handler, body):
    """Kimi's own verbatim `functions.<name>:<idx>` id on the ANTHROPIC
    dialect -- the decoder never touches an id, so this must pass through
    completely unchanged end to end."""
    _start_sse(handler)
    for ev in (
        {"type": "message_start", "message": {"id": "msg_kimi", "type": "message", "role": "assistant",
         "content": [], "model": body.get("model", "claude"), "usage": _usage_a()}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "functions.Read:0", "name": "Read", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"file_path":"pong.txt"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": _usage_a()},
        {"type": "message_stop"},
    ):
        _sse_anthropic_event(handler, ev)
    _end_sse(handler)


def _ascn_thinking_then_tool_then_reply(handler, body):
    """V2a open question 1, anthropic dialect: turn 1 (no `tool_result` in
    the incoming messages yet) answers with thinking+signature and a Read
    tool_use; turn 2 (the replayed request already carries the tool_result,
    and -- if the harness replayed it correctly -- the signed thinking
    block back too) answers plainly. Dispatches on content, never call
    count, so it is retry-ladder-safe exactly like mock_anthropic.py's own
    twin of this scenario."""
    has_tool_result = any(
        isinstance(m.get("content"), list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
        for m in (body.get("messages") or []) if isinstance(m, dict)
    )
    if has_tool_result:
        _start_sse(handler)
        for ev in (
            {"type": "message_start", "message": {"id": "msg_replay_final", "type": "message", "role": "assistant",
             "content": [], "model": body.get("model", "claude"), "usage": _usage_a(input_tokens=900)}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ):
            _sse_anthropic_event(handler, ev)
        _end_sse(handler)
        return
    _start_sse(handler)
    for ev in (
        {"type": "message_start", "message": {"id": "msg_replay_think_tool", "type": "message", "role": "assistant",
         "content": [], "model": body.get("model", "claude"), "usage": _usage_a(input_tokens=321)}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "need to read the file first"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig_replay_1"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu_replay", "name": "Read", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"file_path"'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ':"pong.txt"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 18}},
        {"type": "message_stop"},
    ):
        _sse_anthropic_event(handler, ev)
    _end_sse(handler)


def _ascn_401(handler, body):
    _send_json(handler, 401, {"error_code": "PERMISSION_DENIED", "message": "Invalid access token."})


def _ascn_403_ip(handler, body):
    _send_json(handler, 403, {"error_code": "PERMISSION_DENIED",
                               "message": "Public internet access is not allowed for this workspace; "
                                          "your IP address is not on the IP access list."})


def _ascn_404(handler, body):
    _send_json(handler, 404, {"type": "error", "error": {"type": "not_found_error", "message": "not found"}})


def _ascn_overflow_400(handler, body):
    _send_json(handler, 400, {"type": "error", "error": {
        "type": "invalid_request_error", "message": "prompt is too long: 210000 tokens > 200000 maximum"}})


def _ascn_rate_limit_429(handler, body):
    _send_json(handler, 429, {"type": "error", "error": {
        "type": "rate_limit_error", "message": "Number of request tokens has exceeded your rate limit"}},
        {"retry-after": "3"})


def _ascn_500(handler, body):
    _send_json(handler, 500, {"type": "error", "error": {"type": "api_error", "message": "internal error"}})


ANTHROPIC_SCENARIOS = {
    "thinking-and-signature": _ascn_thinking_and_signature,
    "tool-use": _ascn_tool_use,
    "kimi-tool-id": _ascn_kimi_tool_id,
    "thinking-then-tool-then-reply": _ascn_thinking_then_tool_then_reply,
    "401-error": _ascn_401,
    "403-ip-error": _ascn_403_ip,
    "404-error": _ascn_404,
    "overflow-400": _ascn_overflow_400,
    "rate-limit-429": _ascn_rate_limit_429,
    "500-error": _ascn_500,
    "ok": _finish_anthropic,
}


def _dispatch_anthropic(handler, body: dict) -> None:
    """Longest-suffix match of `body["model"]` against ANTHROPIC_SCENARIOS,
    same convention as the openai-chat SCENARIOS dispatch below -- default
    (no match) is `_finish_anthropic`'s plain pong, byte-identical to the
    pre-V2a behavior every existing test already relies on."""
    model = body.get("model", "") or ""
    for name in sorted(ANTHROPIC_SCENARIOS, key=len, reverse=True):
        if model.endswith(name):
            ANTHROPIC_SCENARIOS[name](handler, body)
            return
    _finish_anthropic(handler, body)


def _scn_tool_call_ok(handler, body):
    """H14 scope H: a streamed OpenAI-shaped tool call (Read) -- the work
    matrix's own `--tools` probe scenario."""
    _finish(handler, [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
            {"index": 0, "id": "call_wm_1", "type": "function", "function": {"name": "Read", "arguments": ""}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"file_path": "pong.txt"}'}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])


def _scn_echo(handler, body):
    """H14 scope F: a non-streaming, plain-JSON scenario that echoes which
    PATH/body this request actually hit -- used to prove end to end which
    of invocations/mlflow/cursor a given model+family combination really
    picked (`chat_route_candidates`), not just that SOME 200 came back."""
    _send_json(handler, 200, {"echo_path": handler.path, "echo_body": body})


# ---------------------------------------------------------------------------
# V2a: error shapes per the brief's own list -- 401/403-IP/404/413/429-with-
# limit_type (already covered by _scn_rate_limit_429 above)/5xx/context
# overflow, plus finish_reason=length-with-minimal-output and Kimi's own
# verbatim `functions.<name>:<idx>` tool-call id shape on the openai-chat
# dialect (mlflow/cursor/invocations all share this same handler).
# ---------------------------------------------------------------------------

def _scn_401(handler, body):
    _send_json(handler, 401, {"error_code": "PERMISSION_DENIED", "message": "Invalid access token."})


def _scn_403_ip(handler, body):
    _send_json(handler, 403, {"error_code": "PERMISSION_DENIED",
                               "message": "Public internet access is not allowed for this workspace; "
                                          "your IP address is not on the IP access list."})


def _scn_404_not_found(handler, body):
    _send_json(handler, 404, {"error_code": "NOT_FOUND", "message": "not found"})


def _scn_500_error(handler, body):
    _send_json(handler, 500, {"error_code": "INTERNAL_ERROR", "message": "internal error, please retry"})


def _scn_context_overflow_400(handler, body):
    """Databricks' own real overflow wording (providers.errors.
    parse_context_overflow's `dbx_full` pattern): "... exceed context
    limit: A + B > L"."""
    _send_json(handler, 400, {"error_code": "BAD_REQUEST",
                               "message": "Input validation error: `inputs` tokens + `max_new_tokens` tokens "
                                          "exceed context limit: 120000 + 16384 > 131072"})


def _scn_413_overflow(handler, body):
    _send_json(handler, 413, {"error_code": "BAD_REQUEST", "message": "request_too_large: payload exceeds limit"})


def _scn_finish_length_minimal(handler, body):
    """finish_reason: length with <=1 output token -- oai_stream.py's own
    `length_with_minimal_output` flag (a provider failure to re-route/
    re-pin, never retried in place; scope C)."""
    _finish(handler, [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "x"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
    ])


def _scn_tool_call_kimi_native_id(handler, body):
    """A tool call whose streamed id is ALREADY Kimi's own native
    `functions.<name>:<idx>` shape -- `hooks.normalize_tool_id`'s
    "kimi_functions_idx" branch must preserve it verbatim, never re-mint."""
    _finish(handler, [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
            {"index": 0, "id": "functions.Read:0", "type": "function", "function": {"name": "Read", "arguments": ""}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"file_path": "pong.txt"}'}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])


def _scn_usage_cached_tokens(handler, body):
    """Cached + reasoning token accounting (scope: "cached tokens where
    reported") -- OpenAI-chat-shaped `usage.prompt_tokens_details.
    cached_tokens`/`completion_tokens_details.reasoning_tokens`."""
    _finish(handler, [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 500, "completion_tokens": 20,
                   "prompt_tokens_details": {"cached_tokens": 300},
                   "completion_tokens_details": {"reasoning_tokens": 8}}},
    ])


def _scn_mlflow_404_then_ok(handler, body):
    """404s ONLY the mlflow sub-path -- proves a `gpt` family endpoint's
    SECOND candidate (cursor) is genuinely reached and answers, rather than
    falling all the way through to invocations."""
    if "/mlflow/" in handler.path:
        _send_json(handler, 404, {"error_code": "NOT_FOUND", "message": "not found"})
        return
    _scn_ok(handler, body)


def _scn_cursor_404_then_ok(handler, body):
    """404s ONLY the cursor sub-path -- the mirror of
    `_scn_mlflow_404_then_ok`, used to simulate a STALE cached route (one
    that used to answer on cursor but no longer does) falling over to
    mlflow, a real "route split" the work matrix's own cache-vs-actual
    fields exist to surface."""
    if "/cursor/" in handler.path:
        _send_json(handler, 404, {"error_code": "NOT_FOUND", "message": "not found"})
        return
    _scn_ok(handler, body)


def _scn_mlflow_or_cursor_404_then_invocations_ok(handler, body):
    """404s ONLY the mlflow/cursor gateway sub-paths, so the FIRST candidate
    a family like glm/gpt tries fails over to the next one -- proves
    `call_databricks_chat`'s own 404-fallback-and-cache dance end to end
    without needing a second, differently-named scenario per candidate
    position."""
    if "/mlflow/" in handler.path or "/cursor/" in handler.path:
        _send_json(handler, 404, {"error_code": "NOT_FOUND", "message": "not found"})
        return
    _scn_ok(handler, body)


def _scn_reasoning_replay_rejected(handler, body):
    """Turn 1: a normal Read tool call (like tool-call-ok); turn 2 (a
    `role: tool` message already present) -- the upstream REJECTS the
    replay with DeepSeek's own reasoning_content-must-be-passed-back
    wording, so a work-matrix probe correctly reports
    `reasoning_replay_ok=False` instead of a false positive."""
    has_tool_msg = any(isinstance(m, dict) and m.get("role") == "tool" for m in (body.get("messages") or []))
    if has_tool_msg:
        _send_json(handler, 400, {"error_code": "BAD_REQUEST",
                                   "message": "The reasoning_content in the thinking mode must be passed back "
                                              "to the API."})
        return
    _scn_tool_call_ok(handler, body)


def _scn_reasoning_replay_loop(handler, body):
    """Two-turn openai-chat "reasoning replay after a tool call" shape (V2a
    open question 1): turn 1 (no `role: tool` message yet) replies with
    reasoning_content + a Read tool call; turn 2 (the replayed request
    already carries the tool result) replies plainly -- lets a caller drive
    a REAL two-turn round trip through the exact request-building code a
    live session would use and observe whether the upstream accepts
    whatever this harness replayed back as reasoning."""
    has_tool_msg = any(isinstance(m, dict) and m.get("role") == "tool" for m in (body.get("messages") or []))
    if has_tool_msg:
        _scn_ok(handler, body)
        return
    _finish(handler, [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "need to read the file"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_replay_1", "type": "function", "function": {"name": "Read", "arguments": ""}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"file_path": "pong.txt"}'}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])


SCENARIOS = {
    "reasoning-content-shape": _scn_reasoning_content_shape,
    "reasoning-blocks-shape": _scn_reasoning_blocks_shape,
    "rate-limit-429": _scn_rate_limit_429,
    "echo": _scn_echo,
    "tool-call-ok": _scn_tool_call_ok,
    "401-error": _scn_401,
    "403-ip-error": _scn_403_ip,
    "404-not-found": _scn_404_not_found,
    "500-error": _scn_500_error,
    "context-overflow-400": _scn_context_overflow_400,
    "413-overflow": _scn_413_overflow,
    "finish-length-minimal": _scn_finish_length_minimal,
    "tool-call-kimi-native-id": _scn_tool_call_kimi_native_id,
    "usage-cached-tokens": _scn_usage_cached_tokens,
    "mlflow-or-cursor-404-then-ok": _scn_mlflow_or_cursor_404_then_invocations_ok,
    "mlflow-404-then-ok": _scn_mlflow_404_then_ok,
    "cursor-404-then-ok": _scn_cursor_404_then_ok,
    "reasoning-replay-loop": _scn_reasoning_replay_loop,
    "reasoning-replay-rejected": _scn_reasoning_replay_rejected,
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
            mock.requests.append({"path": self.path, "body": body,
                                   "headers": {k: v for k, v in self.headers.items()}})

        # H14 scope H: the native Anthropic-Messages path (Claude/GLM/Kimi
        # probed on the anthropic gateway too, `--both`) is a DIFFERENT wire
        # shape entirely (no OpenAI-chat allowlist applies) -- served here,
        # never falling into the openai-chat scenario dispatch below, so ONE
        # mock server can answer both dialects for the work-matrix probe.
        if self.path.startswith("/ai-gateway/anthropic/v1/messages") or \
                self.path.startswith("/serving-endpoints/anthropic/v1/messages"):
            _dispatch_anthropic(self, body)
            return

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
        mock: MockDatabricks = self.server.mock  # type: ignore[attr-defined]
        if self.path.startswith("/api/2.0/serving-endpoints"):
            with mock._lock:
                mock.requests.append({"path": self.path, "body": None,
                                       "headers": {k: v for k, v in self.headers.items()}})
            status = mock.endpoints_status
            body = mock.endpoints_body
            if body is None:
                body = {"endpoints": mock.endpoints_catalog} if status == 200 else {
                    "error_code": "MOCK_ERROR", "message": "mock endpoints error"}
            _send_json(self, status, body)
            return
        _send_json(self, 404, {"error": "mock databricks only serves POST"})


class MockDatabricks:
    def __init__(self):
        self._server = None
        self._thread = None
        self.requests: list = []
        self._lock = threading.Lock()
        # H14 scope E/F/H: GET /api/2.0/serving-endpoints -- 200 with
        # `endpoints_catalog` (a list of endpoint dicts, the shape
        # `probe_databricks_endpoints_full` parses) by default; a test sets
        # `endpoints_status`/`endpoints_body` to simulate 401/403-IP/
        # 403-other/404 (see `set_endpoints_error` below for the common
        # cases' exact wording).
        self.endpoints_status = 200
        self.endpoints_body = None
        self.endpoints_catalog: list = []

    def set_endpoints_catalog(self, endpoints: list) -> None:
        self.endpoints_status = 200
        self.endpoints_body = None
        self.endpoints_catalog = endpoints

    def set_endpoints_error(self, kind: str) -> None:
        """`kind` in "401"/"403-ip"/"403-other"/"404" -- the four cases
        `doctor --work`'s token-validity check (H14 scope E) distinguishes."""
        table = {
            "401": (401, {"error_code": "PERMISSION_DENIED", "message": "Invalid access token."}),
            "403-ip": (403, {"error_code": "PERMISSION_DENIED",
                              "message": "Public internet access is not allowed for this workspace; "
                                         "your IP address is not on the IP access list."}),
            "403-other": (403, {"error_code": "PERMISSION_DENIED",
                                 "message": "User does not have permission to list serving endpoints."}),
            "404": (404, {"error_code": "NOT_FOUND", "message": "not found"}),
        }
        status, body = table[kind]
        self.endpoints_status = status
        self.endpoints_body = body

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

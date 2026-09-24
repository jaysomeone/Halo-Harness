"""rolo_claude.providers.oai_stream -- OpenAIStreamToAnthropic, the state
machine converting OpenAI-dialect streaming chunks into Anthropic SSE
events, plus MessageCollector (non-streaming JSON responses) and the small
event-shape helpers. Moved out of bridge.py unchanged in the H0 package
split; see wip/SIGNATURES.md part3.
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from rolo_claude.providers.config import jdumps
from rolo_claude.providers.errors import _coerce_text, flatten_content_parts

log = logging.getLogger("bridge")

class OpenAIStreamToAnthropic:
    """State machine converting OpenAI streaming chunks to Anthropic SSE events.

    `capture_reasoning`/`strict_tool_json` (H1 scope C, both default False so
    `bridge.py`'s own proxy path -- which never passes them -- is
    byte-for-byte unchanged): when True, the harness's request/loop layer
    gets `self.reasoning_text`/`self.reasoning_details` (raw upstream
    reasoning, both wire shapes) and length-truncated tool calls are kept
    (tagged `_truncated_by_length`) and malformed JSON is tagged
    `_malformed_json` instead of being silently dropped/swallowed to `{}`."""
    def __init__(self, requested_model: str, input_tokens_estimate: int, msg_id: str | None = None,
                 capture_reasoning: bool = False, strict_tool_json: bool = False):
        self.msg_id = msg_id or f"msg_{uuid.uuid4().hex[:24]}"
        self.requested_model = requested_model
        self.input_tokens_estimate = input_tokens_estimate
        self.capture_reasoning = capture_reasoning
        self.strict_tool_json = strict_tool_json
        self.reasoning_text = ""
        self.reasoning_details: list | None = None
        self.tool_call_flags: dict = {}
        self.length_with_minimal_output = False
        self.next_index = 0
        self.text_index = None
        self.text_open = False
        self.tool_order = []
        self.tool_buf = {}
        self._id_to_key = {}
        self._last_key = None
        self._next_auto = 0
        self.finish_reason = None
        self.usage = {}
        self.done = False
        # Rough proxy for output size (chars of text + tool-call argument
        # fragments actually emitted), used ONLY as a fallback estimate for
        # message_delta.usage.output_tokens when the upstream never sends a
        # real "usage" chunk (several dialects/scenarios never do -- but an
        # Anthropic SDK-shaped stream must always carry a non-null int here).
        self._output_units = 0

    def message_start_event(self) -> dict:
        """Return message_start event with usage based on input estimate."""
        return {
            "type": "message_start",
            "message": {
                "id": self.msg_id,
                "type": "message",
                "role": "assistant",
                "model": self.requested_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": self.input_tokens_estimate,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }

    def _key_for(self, tc: dict) -> tuple:
        """Return a stable key for a tool-call delta dict."""
        idx = tc.get("index")
        if idx is not None:
            key = ("idx", idx)
        else:
            id_ = tc.get("id")
            if id_ is not None:
                if id_ not in self._id_to_key:
                    self._id_to_key[id_] = ("auto", self._next_auto)
                    self._next_auto += 1
                key = self._id_to_key[id_]
            else:
                key = self._last_key or ("auto", self._next_auto)
                if key == ("auto", self._next_auto):
                    self._next_auto += 1
        self._last_key = key
        return key

    def feed_chunk(self, chunk: dict) -> dict:
        """Process one upstream chunk, return {"kind":"events"/"error","events":list}."""
        if "error" in chunk and "choices" not in chunk:
            # finding 6: a mid-stream {"error": "boom"} chunk (bare string,
            # not the usual {"message": ...} dict) crashed this with
            # AttributeError since only dicts support .get() -- this loop
            # only catches OSError, so an uncaught exception here died with
            # neither an error event nor message_stop ever reaching the client.
            err = chunk["error"]
            msg = err.get("message") if isinstance(err, dict) else None
            if not isinstance(msg, str):
                msg = err if isinstance(err, str) else _coerce_text(err)
            return {"kind": "error", "events": [self.error_event(msg)]}

        events = []
        if chunk.get("choices"):
            choice = chunk["choices"][0]
            delta = choice.get("delta") or {}

            # Text content -- OpenAI/OpenRouter send a plain string; Databricks
            # can instead send a list of {"type":"text"|"reasoning",...} parts
            # (serving-endpoints/mlflow responses) -- flatten text parts here
            # and log-only any reasoning part found inside the list, so both
            # shapes funnel through one code path.
            content = delta.get("content")
            if isinstance(content, list):
                if self.capture_reasoning:
                    self.reasoning_text += _extract_reasoning_from_parts(content)
                content = flatten_content_parts(content)
            if content:
                if not self.text_open:
                    self.text_index = self.next_index
                    self.next_index += 1
                    events.append(content_block_start(self.text_index, {"type": "text", "text": ""}))
                    self.text_open = True
                events.append(content_block_delta(self.text_index, {"type": "text_delta", "text": content}))
                self._output_units += len(content)

            # Reasoning content: top-level `reasoning`/`reasoning_content`
            # deltas (DeepSeek/Kimi/GLM/Grok, and Databricks' first shape).
            # Never emitted as an Anthropic event by this state machine --
            # the harness reads `self.reasoning_text` after the stream ends
            # (see agent/loop.py) and decides how/whether to display it;
            # the proxy (capture_reasoning=False, its permanent default)
            # keeps its original log-only behavior exactly.
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            if reasoning:
                if self.capture_reasoning:
                    self.reasoning_text += reasoning
                else:
                    log.debug("reasoning delta (not emitted), length=%d", len(reasoning))
            details = delta.get("reasoning_details")
            if details and self.capture_reasoning:
                # OpenRouter unifies reasoning under `reasoning_details[]`;
                # treated as a snapshot of the current full array (verbatim,
                # never trimmed/reordered) rather than incrementally
                # appended -- OpenRouter's own delta shape for this field is
                # under-documented for streaming, and "last non-null wins"
                # is safe for both a single final snapshot and a stream that
                # never touches it until the end.
                self.reasoning_details = details

            # Tool calls
            for tc in delta.get("tool_calls") or []:
                key = self._key_for(tc)
                if key not in self.tool_buf:
                    self.tool_buf[key] = {"name": None, "args": ""}
                    if key not in self.tool_order:
                        self.tool_order.append(key)
                buf = self.tool_buf[key]
                func = tc.get("function") or {}
                if "name" in func:
                    buf["name"] = func["name"]
                if "arguments" in func:
                    buf["args"] += func["arguments"]
                    self._output_units += len(func["arguments"])

            # Finish reason
            fr = choice.get("finish_reason")
            if fr is not None:
                self.finish_reason = fr

        # Usage
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.usage.update(map_usage(usage))

        return {"kind": "events", "events": events}

    def feed_sse_line(self, line: str) -> dict:
        """Parse one SSE line, return {"kind":"events"/"done","events":list}."""
        if not line or line.startswith(":"):
            return {"kind": "events", "events": []}

        # Strip "data:" prefix if present
        if line.startswith("data:"):
            payload = line[5:].lstrip()
        else:
            payload = line

        if payload == "[DONE]":
            return {"kind": "done", "events": self._finalize()}

        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            return {"kind": "events", "events": []}
        return self.feed_chunk(chunk)

    def on_eof(self) -> list[dict]:
        """Call when upstream stream ends; returns final events if not already finalized."""
        return [] if self.done else self._finalize()

    def error_event(self, message: str, err_type: str = "overloaded_error") -> dict:
        """Return an error event dict."""
        return {"type": "error", "error": {"type": err_type, "message": message}}

    def _finalize(self) -> list[dict]:
        """Finalize all pending blocks, return list of events. Idempotent."""
        if self.done:
            return []
        self.done = True

        events = []
        if self.text_open:
            events.append(content_block_stop(self.text_index))

        truncated_key = None
        if self.finish_reason == "length" and self.tool_order:
            if self.strict_tool_json:
                # Keep it (tagged below) instead of silently dropping it --
                # the loop decides whether to ask the model to split the
                # call, distinct from a malformed-but-COMPLETE call (scope C).
                truncated_key = self.tool_order[-1]
            else:
                self.tool_order = self.tool_order[:-1]  # proxy's original behavior, unchanged

        # Emit tool calls. `self.tool_call_flags` (keyed by the minted toolu_
        # id, read by the caller after the stream ends -- never put on the
        # wire event stream itself, which stays exactly Anthropic-shaped)
        # carries the length-truncation/malformed-JSON distinction scope C
        # asks for; only populated when strict_tool_json is set.
        for key in self.tool_order:
            buf = self.tool_buf[key]
            args_str = buf["args"] or "{}"
            flags: dict = {}
            try:
                parsed = json.loads(args_str) if args_str.strip() else {}
            except json.JSONDecodeError as e:
                if self.strict_tool_json:
                    parsed = {}
                    flags = {"malformed_json": True, "raw_input": args_str, "json_error": str(e)}
                else:
                    log.warning(f"Invalid JSON in tool arguments: {args_str}")
                    parsed = {}
            if key == truncated_key:
                flags = {"truncated_by_length": True, "raw_input": args_str}

            idx = self.next_index
            self.next_index += 1
            tool_id = f"toolu_{uuid.uuid4().hex[:24]}"
            if flags and self.strict_tool_json:
                self.tool_call_flags[tool_id] = flags
            block = {"type": "tool_use", "id": tool_id, "name": buf["name"] or "unknown", "input": {}}
            events.append(content_block_start(idx, block))
            events.append(content_block_delta(idx, {"type": "input_json_delta", "partial_json": json.dumps(parsed)}))
            events.append(content_block_stop(idx))

        stop_reason = decide_stop_reason(self.finish_reason, bool(self.tool_order))
        final_usage = dict(self.usage)
        if not isinstance(final_usage.get("output_tokens"), int) or isinstance(final_usage.get("output_tokens"), bool):
            final_usage["output_tokens"] = max(1, self._output_units // 4)
        # scope C: finish_reason length with <=1 output token is indistinguishable
        # from a normal per-endpoint cap and must be treated as a provider
        # failure to re-route/re-pin, never retried in place.
        self.length_with_minimal_output = (
            self.finish_reason == "length" and isinstance(final_usage.get("output_tokens"), int)
            and final_usage["output_tokens"] <= 1
        )
        harness_meta = None
        if self.capture_reasoning or self.strict_tool_json:
            harness_meta = {
                "reasoning_text": self.reasoning_text or None,
                "reasoning_details": self.reasoning_details,
                "tool_call_flags": self.tool_call_flags,
                "length_with_minimal_output": self.length_with_minimal_output,
            }
        events.append(message_delta_event(stop_reason, final_usage, harness_meta=harness_meta))
        events.append({"type": "message_stop"})
        return events


def _extract_reasoning_from_parts(content) -> str:
    """Databricks reasoning SHAPE 2: `{"type":"reasoning","summary":[{"type":
    "summary_text","text":...}]}` content-list items (Claude/GPT/Gemini
    families there) -- `flatten_content_parts` already walks this shape for
    the TEXT parts and just logs these; this sibling pulls the reasoning
    text out for `capture_reasoning` callers without changing that
    function's own (proxy-shared) behavior at all."""
    if not isinstance(content, list):
        return ""
    out = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "reasoning":
            for s in part.get("summary") or []:
                if isinstance(s, dict):
                    out.append(_coerce_text(s.get("text")))
    return "".join(out)


_THINK_TAG_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_SENTINEL_TOKEN_RE = re.compile(r"<｜end▁of▁sentence｜>|<\|end_of_sentence\|>")


def strip_display_artifacts(text: str) -> str:
    """Strip a leading `<think>...</think>` block and stray end-of-sentence
    sentinel tokens seen leaking into `content` on some OpenRouter endpoints
    (e.g. deepseek-v3.2-exp, deepseek-v4-flash-0731) -- scope C. Applied by
    the LOOP/OUTPUT layer to what's DISPLAYED; the raw text (this function's
    input) is what actually gets logged/stored, so nothing here touches the
    session log."""
    if not text:
        return text
    return _SENTINEL_TOKEN_RE.sub("", _THINK_TAG_RE.sub("", text, count=1))


def decide_stop_reason(finish_reason: str | None, has_tool_calls: bool) -> str:
    """Map upstream finish_reason to Anthropic stop_reason."""
    if finish_reason == "length":
        return "max_tokens"
    if has_tool_calls:
        return "tool_use"
    if finish_reason in (None, "stop", "content_filter"):
        return "end_turn"
    return "end_turn"


def map_usage(u: dict) -> dict:
    """Map OpenAI usage dict to Anthropic fields, dropping None values.
    `reasoning_tokens`/`cache_creation_input_tokens`/`cost` (scope C: OpenRouter
    cost, reasoning tokens, cached tokens) are ADDITIVE keys included only
    when the source actually has them -- an existing caller reading just
    input_tokens/output_tokens/cache_read_input_tokens sees no change."""
    result = {}
    if "prompt_tokens" in u:
        result["input_tokens"] = u["prompt_tokens"]
    if "completion_tokens" in u:
        result["output_tokens"] = u["completion_tokens"]
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached is not None:
        result["cache_read_input_tokens"] = cached
    reasoning_tokens = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if reasoning_tokens is not None:
        result["reasoning_tokens"] = reasoning_tokens
    cache_write = u.get("cache_write_tokens") or u.get("cache_creation_input_tokens")
    if cache_write is not None:
        result["cache_creation_input_tokens"] = cache_write
    if u.get("cost") is not None:
        result["cost"] = u["cost"]
    return result


def content_block_start(index: int, content_block: dict) -> dict:
    """Return content_block_start event."""
    return {"type": "content_block_start", "index": index, "content_block": content_block}


def content_block_delta(index: int, delta: dict) -> dict:
    """Return content_block_delta event."""
    return {"type": "content_block_delta", "index": index, "delta": delta}


def content_block_stop(index: int) -> dict:
    """Return content_block_stop event."""
    return {"type": "content_block_stop", "index": index}


def message_delta_event(stop_reason: str, usage: dict, harness_meta: dict | None = None) -> dict:
    """Return message_delta event with stop_reason and usage.
    `harness_meta` (scope C) is an ADDITIVE key, only ever populated by the
    harness's own capture_reasoning/strict_tool_json opt-ins -- the proxy
    (which never sets either) always gets `harness_meta=None` and this key
    is simply omitted, so its SSE payload to a real Claude Code client is
    byte-for-byte unchanged."""
    ev = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": usage,
    }
    if harness_meta is not None:
        ev["harness_meta"] = harness_meta
    return ev


class MessageCollector:
    """Collect a non‑streaming response into events."""
    @staticmethod
    def to_events(body: dict, sm: OpenAIStreamToAnthropic) -> list[dict]:
        """Convert a plain JSON response body to events via the state machine."""
        choice = body["choices"][0]
        msg = choice.get("message") or {}
        delta = {}
        content = msg.get("content")
        if content is not None:
            delta["content"] = content
        tool_calls = msg.get("tool_calls")
        if tool_calls is not None:
            delta["tool_calls"] = tool_calls
        # A non-streaming (JSON-not-SSE-fallback) response carries reasoning
        # directly on `msg`, not nested under a stream "delta" -- copy it
        # through under the SAME keys feed_chunk already understands, so
        # capture_reasoning behaves identically whether the upstream
        # streamed or not.
        for key in ("reasoning", "reasoning_content", "reasoning_details"):
            if msg.get(key) is not None:
                delta[key] = msg[key]
        chunk = {
            "choices": [{"index": 0, "delta": delta, "finish_reason": choice.get("finish_reason")}],
            "usage": body.get("usage"),
        }
        events = list(sm.feed_chunk(chunk).get("events") or [])
        events += sm.on_eof()
        return events


def sse_frame(event_type: str, data: dict) -> bytes:
    """Return a complete SSE frame bytes."""
    return b"event: " + event_type.encode() + b"\r\ndata: " + jdumps(data) + b"\r\n\r\n"

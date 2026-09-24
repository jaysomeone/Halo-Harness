"""rolo_claude.providers.oai_stream -- OpenAIStreamToAnthropic, the state
machine converting OpenAI-dialect streaming chunks into Anthropic SSE
events, plus MessageCollector (non-streaming JSON responses) and the small
event-shape helpers. Moved out of bridge.py unchanged in the H0 package
split; see wip/SIGNATURES.md part3.
"""

from __future__ import annotations

import json
import logging
import uuid

from rolo_claude.providers.config import jdumps
from rolo_claude.providers.errors import _coerce_text, flatten_content_parts

log = logging.getLogger("bridge")

class OpenAIStreamToAnthropic:
    """State machine converting OpenAI streaming chunks to Anthropic SSE events."""
    def __init__(self, requested_model: str, input_tokens_estimate: int, msg_id: str | None = None):
        self.msg_id = msg_id or f"msg_{uuid.uuid4().hex[:24]}"
        self.requested_model = requested_model
        self.input_tokens_estimate = input_tokens_estimate
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
                content = flatten_content_parts(content)
            if content:
                if not self.text_open:
                    self.text_index = self.next_index
                    self.next_index += 1
                    events.append(content_block_start(self.text_index, {"type": "text", "text": ""}))
                    self.text_open = True
                events.append(content_block_delta(self.text_index, {"type": "text_delta", "text": content}))
                self._output_units += len(content)

            # Reasoning content (log only, never emitted to Claude Code)
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            if reasoning:
                log.debug("reasoning delta (not emitted), length=%d", len(reasoning))

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

        # Drop last tool call if length cutoff
        if self.finish_reason == "length" and self.tool_order:
            self.tool_order = self.tool_order[:-1]

        # Emit tool calls
        for key in self.tool_order:
            buf = self.tool_buf[key]
            args_str = buf["args"] or "{}"
            try:
                parsed = json.loads(args_str) if args_str.strip() else {}
            except json.JSONDecodeError:
                log.warning(f"Invalid JSON in tool arguments: {args_str}")
                parsed = {}

            idx = self.next_index
            self.next_index += 1
            tool_id = f"toolu_{uuid.uuid4().hex[:24]}"
            events.append(content_block_start(idx, {"type": "tool_use", "id": tool_id, "name": buf["name"] or "unknown", "input": {}}))
            events.append(content_block_delta(idx, {"type": "input_json_delta", "partial_json": json.dumps(parsed)}))
            events.append(content_block_stop(idx))

        stop_reason = decide_stop_reason(self.finish_reason, bool(self.tool_order))
        final_usage = dict(self.usage)
        if not isinstance(final_usage.get("output_tokens"), int) or isinstance(final_usage.get("output_tokens"), bool):
            final_usage["output_tokens"] = max(1, self._output_units // 4)
        events.append(message_delta_event(stop_reason, final_usage))
        events.append({"type": "message_stop"})
        return events


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
    """Map OpenAI usage dict to Anthropic fields, dropping None values."""
    result = {}
    if "prompt_tokens" in u:
        result["input_tokens"] = u["prompt_tokens"]
    if "completion_tokens" in u:
        result["output_tokens"] = u["completion_tokens"]
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached is not None:
        result["cache_read_input_tokens"] = cached
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


def message_delta_event(stop_reason: str, usage: dict) -> dict:
    """Return message_delta event with stop_reason and usage."""
    return {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": usage,
    }


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

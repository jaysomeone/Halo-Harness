"""halo_harness.providers.responses_stream -- Halo 2.0.3 round 5i part 1:
ResponsesStreamToAnthropic, the `openai-responses` dialect's own decoder,
translating `POST /v1/responses`' streamed `response.*` SSE events
(`docs/harness/OPENAI-RESEARCH.md` section 2) into the SAME plain
Anthropic-shaped event dicts every other dialect's decoder produces
(`providers.oai_stream.OpenAIStreamToAnthropic`, `providers.ollama_stream.
OllamaStreamToAnthropic`) -- reuses that module's free event-builder
functions (`content_block_start`/`_delta`/`_stop`, `message_delta_event`,
`decide_stop_reason`) unchanged, same as `ollama_stream.py` already does,
rather than re-implementing them a third time.

Every confirmed payload names its own `"type"` field identically to the
SSE `event:` line's name (the mock server, and every other decoder in
this codebase, already treat the two as interchangeable -- `tests.
helpers.mock_openai.validate_anthropic_stream` asserts exactly this for
Halo's OWN outbound SSE), so this decoder reads `data["type"]` and never
needs to track a separate "most recent event: line" state at all.

Text and reasoning are buffered into ONE slot each (never keyed per
output item), same simplification `OpenAIStreamToAnthropic`/
`OllamaStreamToAnthropic` already make for their own dialects -- a single
turn realistically carries at most one message item and one reasoning
item. Tool calls ARE keyed per item (`item_id`), buffered, and only
emitted as Anthropic `tool_use` blocks at `_finalize()` time, exactly like
every other dialect's own decoder.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from halo_harness.providers.oai_stream import (
    content_block_delta, content_block_start, content_block_stop, decide_stop_reason, message_delta_event,
)

log = logging.getLogger("bridge")


def map_responses_usage(usage: dict) -> dict:
    """`docs/harness/OPENAI-RESEARCH.md` section 1: `input_tokens`/
    `output_tokens` plus `output_tokens_details.reasoning_tokens`/`input_
    tokens_details.cached_tokens` -- field NAMES confirmed, no example
    numeric body was fetched this round, so (UNCONFIRMED) this assumes
    the same inclusive-totals convention `oai_stream.map_usage` documents
    for chat completions (reasoning/cached tokens are a SUBSET of output_
    tokens/input_tokens, not additional) rather than guessing the other
    way; degrades gracefully (omits a key) on any shape mismatch."""
    if not isinstance(usage, dict):
        return {}
    out: dict = {}
    input_tokens = usage.get("input_tokens")
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
    if isinstance(cached, int) and cached:
        out["cache_read_input_tokens"] = cached
    if isinstance(input_tokens, int):
        out["input_tokens"] = max(0, input_tokens - (cached if isinstance(cached, int) else 0))
    output_tokens = usage.get("output_tokens")
    reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
    if isinstance(reasoning, int) and reasoning:
        out["reasoning_tokens"] = reasoning
    if isinstance(output_tokens, int):
        out["output_tokens"] = max(0, output_tokens - (reasoning if isinstance(reasoning, int) else 0))
    return out


class ResponsesStreamToAnthropic:
    def __init__(self, requested_model: str, input_tokens_estimate: int, msg_id: Optional[str] = None):
        self.msg_id = msg_id or f"msg_{uuid.uuid4().hex[:24]}"
        self.requested_model = requested_model
        self.input_tokens_estimate = input_tokens_estimate
        self.next_index = 0
        self.text_index: Optional[int] = None
        self.text_open = False
        self.reasoning_text = ""
        self._reasoning_started_emitted = False
        self.tool_order: list = []          # item_id, first-seen order
        self.tool_buf: dict = {}            # item_id -> {"call_id","name","arguments"}
        self.tool_call_flags: dict = {}
        self.incomplete = False             # response.incomplete seen (best-effort "length" mapping)
        self.usage: dict = {}
        self.done = False

    def message_start_event(self) -> dict:
        return {
            "type": "message_start",
            "message": {
                "id": self.msg_id, "type": "message", "role": "assistant",
                "model": self.requested_model, "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": self.input_tokens_estimate, "output_tokens": 1,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            },
        }

    def feed_sse_line(self, line) -> dict:
        """Parse one SSE line; returns `{"kind": "events"/"done"/"error",
        "events": [...]}`. An `event:` line, a blank line, or a comment
        (`:`-prefixed) carries no information this decoder needs (see the
        module docstring) and is skipped; a malformed `data:` payload is
        skipped rather than raising -- a stray line must never crash a
        live stream."""
        if isinstance(line, (bytes, bytearray)):
            line = line.decode("utf-8", "replace")
        line = line.rstrip("\n").rstrip("\r")
        if not line or line.startswith(":") or line.startswith("event:"):
            return {"kind": "events", "events": []}
        payload = line[5:].lstrip() if line.startswith("data:") else line
        if not payload or payload == "[DONE]":
            return {"kind": "events", "events": []}
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            log.debug("responses: non-JSON SSE data line skipped: %r", payload[:200])
            return {"kind": "events", "events": []}
        return self._feed_event(obj) if isinstance(obj, dict) else {"kind": "events", "events": []}

    def on_eof(self) -> list:
        return [] if self.done else self._finalize()

    def error_event(self, message: str, err_type: str = "overloaded_error") -> dict:
        return {"type": "error", "error": {"type": err_type, "message": message}}

    def _feed_event(self, data: dict) -> dict:
        etype = data.get("type")
        if etype in ("response.created", "response.in_progress"):
            return {"kind": "events", "events": []}

        item = data.get("item") if isinstance(data.get("item"), dict) else None
        if etype == "response.output_item.added" and item is not None and item.get("type") == "function_call":
            item_id = item.get("id") or data.get("item_id") or f"idx{len(self.tool_order)}"
            if item_id not in self.tool_buf:
                self.tool_order.append(item_id)
                self.tool_buf[item_id] = {"call_id": item.get("call_id"), "name": item.get("name"), "arguments": ""}
            return {"kind": "events", "events": []}

        if etype == "response.output_text.delta":
            delta = data.get("delta")
            events = []
            if isinstance(delta, str) and delta:
                if not self.text_open:
                    self.text_index = self.next_index
                    self.next_index += 1
                    events.append(content_block_start(self.text_index, {"type": "text", "text": ""}))
                    self.text_open = True
                events.append(content_block_delta(self.text_index, {"type": "text_delta", "text": delta}))
            return {"kind": "events", "events": events}

        if etype == "response.function_call_arguments.delta":
            item_id = data.get("item_id")
            delta = data.get("delta")
            if item_id in self.tool_buf and isinstance(delta, str):
                self.tool_buf[item_id]["arguments"] += delta
            return {"kind": "events", "events": []}

        if etype == "response.function_call_arguments.done":
            item_id = data.get("item_id")
            args = data.get("arguments")
            if item_id in self.tool_buf and isinstance(args, str):
                self.tool_buf[item_id]["arguments"] = args  # authoritative full string wins over accumulated deltas
            return {"kind": "events", "events": []}

        if etype == "response.output_item.done" and item is not None:
            events = []
            if item.get("type") == "reasoning":
                summary = item.get("summary")
                text = "".join(s.get("text") or "" for s in summary if isinstance(s, dict)) \
                    if isinstance(summary, list) else ""
                if text:
                    self.reasoning_text += text
                    if not self._reasoning_started_emitted:
                        self._reasoning_started_emitted = True
                        events.append({"type": "reasoning_started"})
            elif item.get("type") == "function_call":
                item_id = item.get("id") or data.get("item_id")
                buf = self.tool_buf.get(item_id)
                if buf is not None:
                    if buf.get("call_id") is None:
                        buf["call_id"] = item.get("call_id")
                    if buf.get("name") is None:
                        buf["name"] = item.get("name")
                    args = item.get("arguments")
                    if isinstance(args, str) and args:
                        buf["arguments"] = args
            return {"kind": "events", "events": events}

        if etype == "response.incomplete":
            self.incomplete = True
            return {"kind": "events", "events": []}

        if etype == "response.completed":
            response = data.get("response") if isinstance(data.get("response"), dict) else data
            usage = response.get("usage") if isinstance(response, dict) else None
            if isinstance(usage, dict):
                self.usage.update(map_responses_usage(usage))
            return {"kind": "done", "events": self._finalize()}

        if etype == "response.failed" or etype == "error":
            # UNCONFIRMED which of these two nestings a real `response.
            # failed` event actually uses (docs/harness/OPENAI-RESEARCH.md
            # section 2's own flagged gap) -- checked in order: a
            # top-level "error" (the plain `error` SSE event's own
            # documented shape, reused defensively here), else the
            # `response` object's own "error"/"incomplete_details"
            # (that object's documented REST shape elsewhere).
            err = data.get("error")
            if not isinstance(err, dict):
                response = data.get("response") if isinstance(data.get("response"), dict) else {}
                err = response.get("error") if isinstance(response.get("error"), dict) else None
            msg = (err.get("message") if err else None) or data.get("message") or "the Responses API reported a failure"
            return {"kind": "error", "events": [self.error_event(msg)]}

        return {"kind": "events", "events": []}

    def _finalize(self) -> list:
        if self.done:
            return []
        self.done = True
        events = []
        if self.text_open:
            events.append(content_block_stop(self.text_index))
        for item_id in self.tool_order:
            buf = self.tool_buf[item_id]
            args_str = buf.get("arguments") or "{}"
            try:
                parsed = json.loads(args_str) if args_str.strip() else {}
            except (json.JSONDecodeError, ValueError) as e:
                parsed = {}
                self.tool_call_flags[buf.get("call_id") or item_id] = {
                    "malformed_json": True, "raw_input": args_str, "json_error": str(e)}
            idx = self.next_index
            self.next_index += 1
            tool_id = buf.get("call_id") or f"toolu_{uuid.uuid4().hex[:24]}"
            block = {"type": "tool_use", "id": tool_id, "name": buf.get("name") or "unknown", "input": {}}
            events.append(content_block_start(idx, block))
            events.append(content_block_delta(idx, {"type": "input_json_delta", "partial_json": json.dumps(parsed)}))
            events.append(content_block_stop(idx))
        stop_reason = decide_stop_reason("length" if self.incomplete else None, bool(self.tool_order))
        final_usage = dict(self.usage)
        if not isinstance(final_usage.get("output_tokens"), int) or isinstance(final_usage.get("output_tokens"), bool):
            final_usage["output_tokens"] = 1
        harness_meta = {
            "reasoning_text": self.reasoning_text or None,
            "tool_call_flags": self.tool_call_flags,
        }
        events.append(message_delta_event(stop_reason, final_usage, harness_meta=harness_meta))
        events.append({"type": "message_stop"})
        return events

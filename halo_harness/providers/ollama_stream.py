"""halo_harness.providers.ollama_stream -- Halo 2.0.3 round 2: the NDJSON
streaming decoder for the `ol:` dialect, translating Ollama's native
`/api/chat` stream (one complete JSON object per line, research doc Q1 --
never SSE framing) into the SAME plain Anthropic-shaped event dicts every
other dialect's decoder produces (`providers.oai_stream.
OpenAIStreamToAnthropic`, `providers.anthropic_sse.AnthropicSSEDecoder`),
so `providers.stream`'s phase-2 loop and everything downstream of it
(agent/loop.py) never need a second code path for this one provider.

Reuses `providers.oai_stream`'s free event-builder functions
(`content_block_start`/`_delta`/`_stop`, `message_delta_event`,
`decide_stop_reason`) unchanged -- those build a generic Anthropic event
shape, not an OpenAI-specific one -- rather than re-implementing the same
small functions a third time.
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


class OllamaStreamToAnthropic:
    """Feed it NDJSON lines one at a time via `feed_line`; `.done` becomes
    True once a line carrying `"done": true` has been processed (or
    `on_eof()`/`error_event()` was called). Text is streamed live
    (`content_block_start` the first time `message.content` is non-empty on
    some line, then a delta per line, `content_block_stop` at finalize);
    `message.thinking` is accumulated into `self.reasoning_text` for
    DISPLAY ONLY -- never emitted as a native Anthropic `thinking` content
    block (there is no signature concept here) and never replayed on a
    later request (`providers.profiles`'s "ollama" branch sets
    `reasoning_replay="empty"` for exactly this reason). Tool calls are
    buffered and only emitted at `_finalize()` time -- same as every other
    dialect's own decoder (`OpenAIStreamToAnthropic._finalize` does the
    identical thing for its own dialect) -- with Halo synthesizing a stable
    `toolu_` id the moment a given 0-based `index` is first seen (research
    doc Q1: Ollama sends no id of its own, only `index`); that id never
    changes again for the lifetime of this one decoder instance (one
    streamed turn), which is what "stable" means for a tool-call id that
    Ollama's own wire format never needs back (replay keys off `tool_name`,
    not an id -- see `providers.ollama_request`)."""

    def __init__(self, requested_model: str, input_tokens_estimate: int, msg_id: Optional[str] = None):
        self.msg_id = msg_id or f"msg_{uuid.uuid4().hex[:24]}"
        self.requested_model = requested_model
        self.input_tokens_estimate = input_tokens_estimate
        self.next_index = 0
        self.text_index: Optional[int] = None
        self.text_open = False
        self.reasoning_text = ""
        self._reasoning_started_emitted = False
        self.tool_order: list = []   # 0-based indices, first-seen order
        self.tool_buf: dict = {}     # index -> {"name": str|None, "arguments": dict, "id": str|None}
        # Round 5b part 2 (brief item 2, "repair loop"): keyed by the
        # SYNTHESIZED toolu_ id (same key `agent/repair.py`'s
        # `tool_call_flags` lookups already use for every other dialect),
        # `{"malformed_json": True, "raw_input": str, "json_error": str}`
        # -- Ollama's own API documents `function.arguments` as "a parsed
        # JSON object, not a string" (research doc Q1), so this branch is
        # rare (a server bug, or a model whose template emits the field as
        # a string that then fails to parse) rather than the common case
        # oai_stream.py's OWN strict_tool_json capture handles -- but
        # recording it here, instead of silently defaulting to `{}` the
        # way this module did before this round, is what lets `agent/
        # loop.py`'s existing `classify_length_tool_call`/`_resolve_tool_
        # call` machinery (shared with every other dialect) see it at all.
        self.tool_call_flags: dict = {}
        # True the moment ANY content/thinking/tool-call fragment has been
        # seen -- `providers.stream.stream_ollama_completion`'s one-time
        # "load" retry only ever fires while this is still False (nothing
        # shown to the caller yet makes a silent retry safe).
        self.any_output_emitted = False
        self.done_reason: Optional[str] = None
        self._timing: dict = {}
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

    def _tool_id_for(self, index: int) -> str:
        buf = self.tool_buf.setdefault(index, {"name": None, "arguments": {}, "id": None})
        if buf["id"] is None:
            buf["id"] = f"toolu_{uuid.uuid4().hex[:24]}"
        return buf["id"]

    def feed_line(self, line) -> list:
        """Parse one NDJSON line; returns the events it produced (possibly
        `[]`). A blank line, or one that fails to parse as a JSON object,
        is skipped rather than raising -- Ollama's own stream is one
        complete JSON object per line with no blank-line/SSE framing, so
        neither case should occur in practice, but a stray line must never
        crash a live stream."""
        if isinstance(line, (bytes, bytearray)):
            line = line.decode("utf-8", "replace")
        line = line.strip()
        if not line:
            return []
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            log.debug("ollama: non-JSON NDJSON line skipped: %r", line[:200])
            return []
        return self._feed_chunk(chunk) if isinstance(chunk, dict) else []

    def on_eof(self) -> list:
        """A clean EOF with no final `"done": true` line ever seen (a
        connection that closed mid-stream) -- finalize with whatever was
        buffered so far rather than hanging the generator forever."""
        return [] if self.done else self._finalize()

    def error_event(self, message: str, err_type: str = "overloaded_error") -> dict:
        return {"type": "error", "error": {"type": err_type, "message": message}}

    def _feed_chunk(self, chunk: dict) -> list:
        events = []
        message = chunk.get("message") or {}

        content = message.get("content")
        if isinstance(content, str) and content:
            self.any_output_emitted = True
            if not self.text_open:
                self.text_index = self.next_index
                self.next_index += 1
                events.append(content_block_start(self.text_index, {"type": "text", "text": ""}))
                self.text_open = True
            events.append(content_block_delta(self.text_index, {"type": "text_delta", "text": content}))

        thinking = message.get("thinking")
        if isinstance(thinking, str) and thinking:
            self.any_output_emitted = True
            self.reasoning_text += thinking
            if not self._reasoning_started_emitted:
                self._reasoning_started_emitted = True
                events.append({"type": "reasoning_started"})

        for tc in message.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            self.any_output_emitted = True
            index = tc.get("index")
            index = index if isinstance(index, int) else len(self.tool_order)
            if index not in self.tool_order:
                self.tool_order.append(index)
            self._tool_id_for(index)  # mints the id the FIRST time this index is seen
            func = tc.get("function") or {}
            if isinstance(func.get("name"), str) and func["name"]:
                self.tool_buf[index]["name"] = func["name"]
            args = func.get("arguments")
            if isinstance(args, dict):
                # Overwrite, never concatenate: research doc Q1 confirms
                # Ollama sends arguments as an already-parsed object, not an
                # incremental JSON-text fragment the way OpenAI's SSE
                # tool-call deltas do -- "last one wins" is correct whether
                # a given index arrives once or (conservatively) more than
                # once with a growing object.
                self.tool_buf[index]["arguments"] = args
            elif isinstance(args, str):
                try:
                    parsed = json.loads(args) if args.strip() else {}
                except (json.JSONDecodeError, ValueError) as e:
                    parsed = {}
                    self.tool_call_flags[self.tool_buf[index]["id"]] = {
                        "malformed_json": True, "raw_input": args, "json_error": str(e),
                    }
                self.tool_buf[index]["arguments"] = parsed

        if chunk.get("done"):
            self.done_reason = chunk.get("done_reason")
            for key in ("prompt_eval_count", "eval_count", "prompt_eval_duration",
                        "eval_duration", "load_duration", "total_duration"):
                if key in chunk:
                    self._timing[key] = chunk[key]
            events.extend(self._finalize())
        return events

    def _finalize(self) -> list:
        """Idempotent, same contract as every other dialect's own
        finalize. `done_reason: "length"` maps through the SAME
        `decide_stop_reason` every other dialect uses (brief: "Map
        done_reason: 'length' to the existing length-stop handling")."""
        if self.done:
            return []
        self.done = True
        events = []
        if self.text_open:
            events.append(content_block_stop(self.text_index))
        for index in self.tool_order:
            buf = self.tool_buf[index]
            idx = self.next_index
            self.next_index += 1
            block = {"type": "tool_use", "id": buf["id"], "name": buf["name"] or "unknown", "input": {}}
            events.append(content_block_start(idx, block))
            events.append(content_block_delta(
                idx, {"type": "input_json_delta", "partial_json": json.dumps(buf["arguments"])}))
            events.append(content_block_stop(idx))
        stop_reason = decide_stop_reason(self.done_reason, bool(self.tool_order))
        prompt_tokens = self._timing.get("prompt_eval_count")
        completion_tokens = self._timing.get("eval_count")
        usage = {
            "input_tokens": prompt_tokens if isinstance(prompt_tokens, int) else self.input_tokens_estimate,
            "output_tokens": completion_tokens if isinstance(completion_tokens, int) else 1,
        }
        harness_meta = {
            "done_reason": self.done_reason,
            "reasoning_text": self.reasoning_text or None,
            "timing_ns": dict(self._timing),
            "tool_call_flags": dict(self.tool_call_flags),
        }
        events.append(message_delta_event(stop_reason, usage, harness_meta=harness_meta))
        events.append({"type": "message_stop"})
        return events

"""halo_harness.providers.oai_stream -- OpenAIStreamToAnthropic, the state
machine converting OpenAI-dialect streaming chunks into Anthropic SSE
events, plus MessageCollector (non-streaming JSON responses) and the small
event-shape helpers. Moved out of bridge.py unchanged in the H0 package
split; see wip/SIGNATURES.md part3.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from halo_harness.providers.config import jdumps
from halo_harness.providers.errors import _coerce_text, flatten_content_parts
from halo_harness.providers.hooks import (
    args_repair, normalize_tool_id, stream_aggregate_apply, stream_aggregate_key, think_tag_strip,
)

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
                 capture_reasoning: bool = False, strict_tool_json: bool = False,
                 tool_id_format: str = "mint", kimi_tool_id_start: int = 0):
        self.msg_id = msg_id or f"msg_{uuid.uuid4().hex[:24]}"
        self.requested_model = requested_model
        self.input_tokens_estimate = input_tokens_estimate
        self.capture_reasoning = capture_reasoning
        self.strict_tool_json = strict_tool_json
        # H2 finding 1: which row-driven id transform to apply at finalize
        # time (providers/hooks.py.normalize_tool_id) -- "preserve" (the
        # proxy's permanent default) keeps this byte-for-byte unchanged.
        self.tool_id_format = tool_id_format
        self.reasoning_text = ""
        self.reasoning_details: list | None = None
        self._reasoning_details_by_index: dict = {}  # finding 2: merge state, by (type,index)
        # Halo 2.0.1 W2a telemetry (GLM-brief.md item 6 / HALO-2.0.1-
        # liveness-tips-brief.md Part A6): `reasoning_chunk_count` counts
        # DISTINCT `feed_chunk` calls that carried new reasoning content --
        # ">1" is this harness's own signal for "the gateway streamed
        # reasoning incrementally on the wire" (as opposed to one lump,
        # early or late) feeding `reasoning_streamed` in the session log.
        # `first_reasoning_wall`/`first_tool_wall` are absolute
        # `time.monotonic()` reads (never a DURATION -- the caller,
        # `agent/loop.py::Session._step`, subtracts its own `call_t0`) the
        # first time THIS stream sees reasoning/a tool-call delta -- real
        # wire-arrival time, independent of when `_finalize` later
        # synthesizes the one-shot Anthropic-shaped events for them.
        self.reasoning_chunk_count = 0
        self.first_reasoning_wall: float | None = None
        self.first_tool_wall: float | None = None
        self.tool_call_flags: dict = {}
        self.length_with_minimal_output = False
        self.next_index = 0
        self.text_index = None
        self.text_open = False
        self.tool_order = []
        self.tool_buf = {}
        self._id_to_key = {}
        self._last_key_box = [None]  # boxed: hooks.stream_aggregate_key mutates these in place
        self._next_auto_box = [0]
        # H9 critical review finding 1: seeded from the HIGHEST
        # `functions.{name}:{idx}` id already logged this session (agent/
        # invariants.highest_kimi_functions_idx + 1), never a bare 0 -- a
        # fresh per-stream counter that always restarted at 0 collided with
        # ids minted several turns ago (see that function's own docstring).
        self._kimi_counter_box = [kimi_tool_id_start]
        self.finish_reason = None
        self.usage = {}
        self.done = False
        # H10 Part A: OpenRouter stamps a top-level `"provider"` key on
        # every chunk naming which backing inference provider actually
        # served this request (e.g. "DeepInfra", "Novita") -- captured
        # here, unconditionally (costs nothing when a chunk doesn't carry
        # it), and surfaced via `_finalize`'s own harness_meta so
        # `agent/loop.py` can log it as the `usage` node's `provider` field.
        self.responding_provider: str | None = None
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
        """Return a stable key for a tool-call delta dict -- delegates to
        `hooks.stream_aggregate_key` (finding 11: an index-less delta
        always continues the CURRENT call, never opens a new one by id)."""
        return stream_aggregate_key(
            tc.get("index"), tc.get("id"),
            id_to_key=self._id_to_key, next_auto=self._next_auto_box, last_key=self._last_key_box,
        )

    def _note_reasoning_chunk(self) -> bool:
        """Halo 2.0.1 W2a: called once per `feed_chunk` invocation that
        carried NEW, non-empty reasoning content (any of the three wire
        shapes) -- `reasoning_chunk_count` counts how many SEPARATE chunks
        contributed, and `first_reasoning_wall` stamps the first one, both
        read back by `agent/loop.py::Session._step` after the stream ends
        (`_finalize`'s own `harness_meta`, below). Returns True iff this is
        the FIRST reasoning chunk this stream has ever seen -- each call
        site uses that to append a one-time `{"type": "reasoning_started"}`
        marker to `feed_chunk`'s own `events` list, the REAL-TIME signal
        `agent/loop.py::Session._step` needs (this module never emits an
        Anthropic-shaped event for the reasoning CONTENT itself -- see
        `feed_chunk`'s own comment -- so without this marker the harness
        would have no way to know reasoning started until the whole stream
        finishes)."""
        is_first = self.reasoning_chunk_count == 0
        self.reasoning_chunk_count += 1
        if self.first_reasoning_wall is None:
            self.first_reasoning_wall = time.monotonic()
        return is_first

    def _merge_reasoning_details_delta(self, details: list) -> None:
        """Merge one SSE chunk's `reasoning_details` fragment array into
        `self._reasoning_details_by_index` by (type, index) -- see
        feed_chunk's call site for why (finding 2). Each incoming entry is
        matched to its existing merged entry by `index` (defaulting to 0
        when the upstream omits it, e.g. a single-block non-parallel
        stream); a TYPE CHANGE at the same index starts a fresh entry
        rather than concatenating text of two different kinds together.
        `text`/`summary`/`data` (whichever the entry actually carries) are
        concatenated; `id`/`format`/`signature` take the latest non-null
        value, needed verbatim for OpenRouter's signed/encrypted formats
        (Gemini `thought_signature`, Grok `encrypted_content`)."""
        for entry in details:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("index")
            idx = idx if isinstance(idx, int) else 0
            existing = self._reasoning_details_by_index.get(idx)
            if existing is None or existing.get("type") != entry.get("type"):
                self._reasoning_details_by_index[idx] = dict(entry)
                continue
            for text_key in ("text", "summary", "data"):
                if text_key in entry:
                    prev = existing.get(text_key)
                    piece = entry[text_key]
                    if isinstance(prev, str) and isinstance(piece, str):
                        existing[text_key] = prev + piece
                    elif isinstance(piece, str):
                        existing[text_key] = piece
            for meta_key in ("id", "format", "signature"):
                if entry.get(meta_key) is not None:
                    existing[meta_key] = entry[meta_key]
        self.reasoning_details = [self._reasoning_details_by_index[i]
                                   for i in sorted(self._reasoning_details_by_index)]

    def feed_chunk(self, chunk: dict) -> dict:
        """Process one upstream chunk, return {"kind":"events"/"error","events":list}."""
        provider = chunk.get("provider")
        if isinstance(provider, str) and provider:
            self.responding_provider = provider
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
                    parts_reasoning = _extract_reasoning_from_parts(content)
                    if parts_reasoning:
                        self.reasoning_text += parts_reasoning
                        if self._note_reasoning_chunk():
                            events.append({"type": "reasoning_started"})
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
                    if self._note_reasoning_chunk():
                        events.append({"type": "reasoning_started"})
                else:
                    log.debug("reasoning delta (not emitted), length=%d", len(reasoning))
            details = delta.get("reasoning_details")
            if details and self.capture_reasoning:
                # Finding 2: OpenRouter streams reasoning_details as
                # PER-CHUNK FRAGMENTS (Roo and the AI-SDK provider merge
                # them by type+index) -- "last chunk wins" silently
                # truncated everything but the final fragment. Merge by
                # (type, index): concatenate text/summary/data, keep the
                # latest non-null id/format/signature.
                self._merge_reasoning_details_delta(details)
                if self._note_reasoning_chunk():
                    events.append({"type": "reasoning_started"})

            # Tool calls
            for tc in delta.get("tool_calls") or []:
                if self.first_tool_wall is None:
                    self.first_tool_wall = time.monotonic()
                    if self.strict_tool_json:
                        events.append({"type": "tool_started"})
                key = self._key_for(tc)
                if key not in self.tool_buf:
                    self.tool_buf[key] = {"name": None, "args": "", "raw_id": None}
                    if key not in self.tool_order:
                        self.tool_order.append(key)
                buf = self.tool_buf[key]
                # Finding 1: keep the FIRST non-empty upstream id per key,
                # never overwritten by a later None/empty delta on the same
                # key -- this is what every LATER request replays verbatim.
                tc_id = tc.get("id")
                if isinstance(tc_id, str) and tc_id and buf["raw_id"] is None:
                    buf["raw_id"] = tc_id
                func = tc.get("function") or {}
                self._output_units += stream_aggregate_apply(buf, func)

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
                # H2 args_repair: a second, lenient attempt (trailing
                # commas, Python-repr True/False/None, single quotes)
                # BEFORE giving up and tagging the call malformed -- a
                # length-truncated call (key == truncated_key) skips this
                # entirely, since a partial call is never "repairable" JSON.
                repaired = args_repair(args_str) if key != truncated_key else None
                if repaired is not None:
                    parsed = repaired
                    # H10 Part A: telemetry's own `repair_kind="lenient_json"`
                    # signal -- args_repair's SECOND, lenient JSON attempt
                    # (trailing commas, Python-repr True/False/None, single
                    # quotes) actually fixed something; distinct from a call
                    # whose JSON parsed cleanly on the first try, which never
                    # reaches this branch at all.
                    flags = {"lenient_json_repaired": True}
                elif self.strict_tool_json:
                    parsed = {}
                    flags = {"malformed_json": True, "raw_input": args_str, "json_error": str(e)}
                else:
                    log.warning(f"Invalid JSON in tool arguments: {args_str}")
                    parsed = {}
            if key == truncated_key:
                flags = {"truncated_by_length": True, "raw_input": args_str}

            idx = self.next_index
            self.next_index += 1
            # Finding 1 + tool_id_normalize: the upstream's own id (kept
            # verbatim per index since the first delta that carried one)
            # wins, row-transformed per profile.tool_id_format; only minted
            # fresh when the upstream never sent one at all.
            tool_id = normalize_tool_id(
                buf.get("raw_id"), name=buf["name"] or "unknown",
                tool_id_format=self.tool_id_format, counter=self._kimi_counter_box,
            ) or f"toolu_{uuid.uuid4().hex[:24]}"
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
        # GLM-brief.md item 3 / W2-plan item 3: two more terminal
        # `finish_reason` values a chat-dialect gateway (Z.ai GLM API,
        # docs.z.ai) can send on an otherwise-clean 200 stream --
        # `model_context_window_exceeded` feeds the existing overflow ->
        # compaction -> retry path (agent/loop.py::Session._step, the SAME
        # way a phase-1 400 overflow does); `sensitive` becomes a clear,
        # never-retried error carrying whatever text DID stream (a refusal
        # explanation, when the gateway sends one as ordinary content).
        # `decide_stop_reason` above leaves both as the ordinary Anthropic-
        # shape "end_turn" -- these two booleans are the real signal.
        model_context_window_exceeded = self.finish_reason == "model_context_window_exceeded"
        sensitive_finish = self.finish_reason == "sensitive"
        harness_meta = None
        if self.capture_reasoning or self.strict_tool_json:
            harness_meta = {
                "reasoning_text": self.reasoning_text or None,
                "reasoning_details": self.reasoning_details,
                "tool_call_flags": self.tool_call_flags,
                "length_with_minimal_output": self.length_with_minimal_output,
                "responding_provider": self.responding_provider,
                "model_context_window_exceeded": model_context_window_exceeded,
                "sensitive_finish": sensitive_finish,
                "reasoning_chunk_count": self.reasoning_chunk_count,
                "first_reasoning_wall": self.first_reasoning_wall,
                "first_tool_wall": self.first_tool_wall,
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


# H2: the real implementation now lives in providers/hooks.py (named hook
# think_tag_strip, called from output.py per finding 15) -- re-exported
# here under its original scope-C name so every existing import of
# `oai_stream.strip_display_artifacts` keeps working unchanged.
strip_display_artifacts = think_tag_strip


def decide_stop_reason(finish_reason: str | None, has_tool_calls: bool) -> str:
    """Map upstream finish_reason to Anthropic stop_reason."""
    if finish_reason == "length":
        return "max_tokens"
    if has_tool_calls:
        return "tool_use"
    if finish_reason in (None, "stop", "content_filter"):
        return "end_turn"
    return "end_turn"


def _int_or_none(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def map_usage(u: dict) -> dict:
    """Map OpenAI usage dict to Anthropic fields, dropping None values.
    `reasoning_tokens`/`cache_creation_input_tokens`/`cost` (scope C: OpenRouter
    cost, reasoning tokens, cached tokens) are ADDITIVE keys included only
    when the source actually has them -- an existing caller reading just
    input_tokens/output_tokens/cache_read_input_tokens sees no change.

    1.0.1 fixpass finding 6: OpenAI's own `prompt_tokens`/`completion_tokens`
    are INCLUSIVE totals -- `prompt_tokens_details.cached_tokens`/
    `completion_tokens_details.reasoning_tokens` are SUBSETS of them, never
    additional tokens on top -- unlike Anthropic's native usage shape, where
    `input_tokens`/`cache_read_input_tokens`/`cache_creation_input_tokens`
    are genuinely disjoint buckets that every downstream consumer (`model.py`
    `CostMeter._fallback_cost`, `agent/loop.py`'s own `_total_prompt_tokens`)
    sums together on exactly that assumption. `input_tokens`/`output_tokens`
    here are therefore the UNCACHED/NON-REASONING remainder (`prompt_tokens`
    minus cached, `completion_tokens` minus reasoning) so those downstream
    sums land on the real total exactly once; `cache_read_input_tokens`/
    `reasoning_tokens` themselves are unchanged -- still the real counts,
    still reported for display/breakdown. Before this fix `input_tokens`/
    `output_tokens` were the full (inclusive) OpenAI totals AND the cached/
    reasoning portion was ALSO added on top by every summing consumer, so a
    cached or reasoning-heavy reply (a Databricks gpt-5/6-family endpoint,
    verified) was billed -- and its context-window usage displayed -- too
    high."""
    result = {}
    prompt_tokens = _int_or_none(u.get("prompt_tokens"))
    cached = _int_or_none((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
    if cached:
        result["cache_read_input_tokens"] = cached
    if "prompt_tokens" in u:
        result["input_tokens"] = max(0, prompt_tokens - cached) if prompt_tokens is not None else u["prompt_tokens"]

    completion_tokens = _int_or_none(u.get("completion_tokens"))
    reasoning_tokens = _int_or_none((u.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0
    if reasoning_tokens:
        result["reasoning_tokens"] = reasoning_tokens
    if "completion_tokens" in u:
        result["output_tokens"] = (max(0, completion_tokens - reasoning_tokens) if completion_tokens is not None
                                    else u["completion_tokens"])

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

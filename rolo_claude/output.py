"""rolo_claude.output -- print-mode ("headless") sinks. `PrintModeSink`
(H0/H1) covers `text`/`json`; `StreamJsonSink` (U0 scope F) adds
`stream-json`: an `init` line, `assistant`/`user` message lines, an optional
`stream_event` line per delta (only with `--include-partial-messages`), and
a final `result` line -- Claude Code's own SDK wire shape.

H2 finding 15: a message's fate (the FINAL one of the turn, vs. an
INTERMEDIATE one a tool loop produced along the way) is only knowable in
ARREARS -- once the NEXT `message_start` arrives (this one was
intermediate) or `turn_done` fires (this one was final) -- so nothing here
streams text live character-by-character anymore; every message is
buffered, then either discarded (an intermediate message, non-verbose),
shown dimmed-and-stripped (an intermediate message, `--verbose`), or
always shown as the turn's `result` (the final message, both modes).
"""

from __future__ import annotations

import json as json_module
import sys
from typing import Iterator, Optional

from rolo_claude import events as ev
from rolo_claude.providers.hooks import think_tag_strip

_CUMULATIVE_USAGE_KEYS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "reasoning_tokens",
)


def _try_structured_output(text: str, json_schema: Optional[str]):
    """`--json-schema`: best-effort re-parse of the final reply text as
    JSON (the schema itself was appended to the system prompt as an
    instruction -- see headless.py -- there is no separate validation
    library dependency here, just "did the model actually return JSON").
    None when `json_schema` wasn't requested, or the text isn't valid
    JSON."""
    if not json_schema or not text:
        return None
    try:
        return json_module.loads(text)
    except ValueError:
        return None


def _merge_usage(total: dict, delta: dict) -> None:
    """Sum numeric usage fields from one model call into the turn's
    running total -- finding 15: a three-call turn must report the WHOLE
    turn's tokens, not just the last call's."""
    if not isinstance(delta, dict):
        return
    for k in _CUMULATIVE_USAGE_KEYS:
        v = delta.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total[k] = total.get(k, 0) + v


def build_result_object(
    *,
    session_id: str,
    model: str,
    num_turns: int,
    stop_reason: Optional[str],
    usage: dict,
    total_cost_usd: Optional[float],
    result_text: str,
    is_error: bool = False,
    subtype: str = "success",
    permission_denials: Optional[list] = None,
    structured_output=None,
) -> dict:
    """The `-p --output-format json` result object -- a subset of plan
    D-TUI's full shape (duration_ms/uuid are TUI/hook concerns that don't
    exist yet in this build). H2 scope D: `permission_denials` is a list of
    `{"tool_name","tool_input","reason"}` -- one entry per `ask`-turned-
    deny outcome print mode hit this turn (D6: "an `ask` outcome -> deny
    with an error tool_result naming the suggested rule" -- this is the
    SAME information surfaced structurally for a `--output-format json`
    caller, empty when nothing was denied for that reason). `structured_output`
    (U0 scope F, `--json-schema`) is the final text re-parsed as JSON, or
    None when `--json-schema` wasn't used or the model's reply wasn't valid
    JSON."""
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result_text,
        "session_id": session_id,
        "num_turns": num_turns,
        "stop_reason": stop_reason,
        "usage": usage or {},
        "total_cost_usd": total_cost_usd,
        "structured_output": structured_output,
        "model": model,
        "permission_denials": permission_denials or [],
    }


class PrintModeSink:
    """Drains a Session.turn(...) event iterator and either prints plain
    text (the FINAL assistant message only, non-verbose; every message
    plus dimmed reasoning, verbose) or accumulates everything and prints
    one JSON result object at the end (cumulative usage/cost across every
    model call the turn made). Returns the process exit code (0 success,
    1 on an error event)."""

    def __init__(self, *, output_format: str = "text", session_id: str = "", model: str = "", stream=None,
                 verbose: bool = False, permission_denials: Optional[list] = None, json_schema: Optional[str] = None,
                 max_budget_usd: Optional[float] = None):
        if output_format not in ("text", "json"):
            raise ValueError(f"unsupported output format: {output_format!r} (H0 supports text|json only)")
        self.json_schema = json_schema
        self.max_budget_usd = max_budget_usd
        self.output_format = output_format
        self.session_id = session_id
        self.model = model
        self.stream = stream or sys.stdout
        self.verbose = verbose
        self._budget_exceeded = False
        # A reference to the CALLER's (agent/loop.py Session's own) list --
        # mutated in place as the turn runs, so it's complete by the time
        # `finish()` reads it after `consume()` has drained the generator.
        self._permission_denials = permission_denials if permission_denials is not None else []
        self._pending_text_parts: list = []
        self._pending_thinking_parts: list = []
        self._final_text = ""  # the LAST message's text, decided once turn_done fires
        self._had_error = False
        self._error_message: Optional[str] = None
        self._stop_reason: Optional[str] = None
        self._usage: dict = {}
        self._total_cost_usd: Optional[float] = None
        self._num_turns = 0
        self._saw_any_message = False

    def consume(self, event_iter: Iterator[ev.Event]) -> int:
        """Safe to call again on the SAME sink for a later turn
        (`--input-format stream-json`, reusing one sink for the whole
        run): `_had_error`/`_error_message` reset here so a prior turn's
        failure never leaks into a later, otherwise-successful turn's
        result."""
        self._had_error = False
        self._error_message = None
        self._budget_exceeded = False
        for event in event_iter:
            self._handle(event)
            if self._budget_exceeded:
                event_iter.close()
                break
        return self.finish()

    def _flush_pending_as_intermediate(self) -> None:
        """A message just ended WITHOUT being the turn's last one (another
        `message_start` followed it) -- shown only under `--verbose`
        (dimmed reasoning, then plain stripped text), discarded otherwise."""
        if not self.verbose:
            self._pending_text_parts = []
            self._pending_thinking_parts = []
            return
        thinking = "".join(self._pending_thinking_parts).strip()
        if thinking and self.output_format == "text":
            self.stream.write("\x1b[2m" + thinking + "\x1b[0m\n")
        text = think_tag_strip("".join(self._pending_text_parts))
        if text and self.output_format == "text":
            self.stream.write(text)
            if not text.endswith("\n"):
                self.stream.write("\n")
        self._pending_text_parts = []
        self._pending_thinking_parts = []

    def _handle(self, event: ev.Event) -> None:
        if event.kind == "message_start":
            if self._saw_any_message:
                self._flush_pending_as_intermediate()
            self._saw_any_message = True
            self._pending_text_parts = []
            self._pending_thinking_parts = []
        elif event.kind == "thinking_delta":
            self._pending_thinking_parts.append(event.data.get("text", ""))
        elif event.kind == "text_delta":
            self._pending_text_parts.append(event.data.get("text", ""))
        elif event.kind == "tool_use_ready":
            # H2 scope D acceptance: "three tool calls visible with
            # --verbose" -- a real Anthropic client shows a tool-use card
            # for every call; text-mode --verbose gets the plain-text
            # equivalent (dimmed, like thinking), never shown non-verbose
            # (the whole point of --verbose here is showing the WORK, not
            # just the final answer).
            if self.verbose and self.output_format == "text":
                name = event.data.get("name", "?")
                tool_input = event.data.get("input") or {}
                summary = json_module.dumps(tool_input, default=str)
                if len(summary) > 200:
                    summary = summary[:200] + "...}"
                repaired = " (repaired)" if event.data.get("repaired") else ""
                self.stream.write(f"\x1b[2m[tool] {name}{repaired} {summary}\x1b[0m\n")
        elif event.kind == "tool_result":
            if self.verbose and self.output_format == "text":
                ok = "ok" if event.data.get("ok") else "error"
                self.stream.write(f"\x1b[2m[tool result: {ok}]\x1b[0m\n")
        elif event.kind == "message_end":
            self._stop_reason = event.data.get("stop_reason")
            _merge_usage(self._usage, event.data.get("usage") or {})
            cost = event.data.get("cost_usd")
            if cost is not None:
                self._total_cost_usd = cost  # already the SESSION's cumulative total (agent/loop.py)
                if self.max_budget_usd is not None and cost >= self.max_budget_usd:
                    self._budget_exceeded = True
                    # `turn_done` (the only other place _final_text is set)
                    # never arrives once consume() breaks out below -- capture
                    # whatever text this message already produced right now.
                    self._final_text = think_tag_strip("".join(self._pending_text_parts))
        elif event.kind == "error":
            self._had_error = True
            self._error_message = event.data.get("message")
        elif event.kind == "turn_done":
            self._num_turns = event.turn
            # finding 15: whatever's still pending here IS the final message.
            self._final_text = think_tag_strip("".join(self._pending_text_parts))
            if self.verbose:
                thinking = "".join(self._pending_thinking_parts).strip()
                if thinking and self.output_format == "text":
                    self.stream.write("\x1b[2m" + thinking + "\x1b[0m\n")
            self._pending_text_parts = []
            self._pending_thinking_parts = []

    def finish(self) -> int:
        is_error = self._had_error or self._budget_exceeded
        if self.output_format == "text":
            if self._budget_exceeded:
                if self._final_text:
                    self.stream.write(self._final_text)
                    if not self._final_text.endswith("\n"):
                        self.stream.write("\n")
                    self.stream.flush()
                print(f"\nerror: --max-budget-usd (${self.max_budget_usd}) reached", file=sys.stderr)
            elif self._had_error:
                print(f"\nerror: {self._error_message}", file=sys.stderr)
            elif self._final_text:
                self.stream.write(self._final_text)
                if not self._final_text.endswith("\n"):
                    self.stream.write("\n")
                self.stream.flush()
        else:
            if self._budget_exceeded:
                subtype = "error_max_budget_usd"
            elif self._had_error:
                subtype = "error_during_execution"
            else:
                subtype = "success"
            obj = build_result_object(
                session_id=self.session_id,
                model=self.model,
                num_turns=self._num_turns,
                stop_reason=self._stop_reason,
                usage=self._usage,
                total_cost_usd=self._total_cost_usd,
                result_text=self._final_text,
                is_error=is_error,
                subtype=subtype,
                permission_denials=self._permission_denials,
                structured_output=_try_structured_output(self._final_text, self.json_schema),
            )
            # write to self.stream (defaults to sys.stdout, same as a bare
            # print() would have -- but this way a caller that passed its
            # OWN stream (e.g. testing.fake_controller.run_demo) gets json
            # output there too, matching the text branch above).
            self.stream.write(json_module.dumps(obj) + "\n")
            self.stream.flush()
        return 1 if is_error else 0


class StreamJsonSink:
    """`--output-format stream-json` (U0 scope F). Emits, in order: one
    `system`/`init` line, then an `assistant`/`user` line per completed
    message/tool-result batch (buffered exactly like `PrintModeSink` --
    finding 15's "only knowable in arrears" applies here too: a message is
    flushed when the NEXT `message_start` or `turn_done` arrives, tool_use
    blocks attached to it from any `tool_use_ready` events seen since its
    own `message_end`), optionally a `stream_event` line per text/thinking
    delta (`--include-partial-messages`), and a final `result` line.
    `max_budget_usd`, when given, stops consuming further events (closing
    the underlying generator) the first time the session's cumulative cost
    (message_end's own `cost_usd`, already the running total) meets or
    exceeds it -- the result comes back `is_error=True`,
    `subtype="error_max_budget_usd"`."""

    def __init__(self, *, session_id: str, cwd: str, model: str, permission_mode: str,
                 tools: Optional[list] = None, mcp_servers: Optional[list] = None,
                 slash_commands: Optional[list] = None, include_partial_messages: bool = False,
                 max_budget_usd: Optional[float] = None, stream=None,
                 permission_denials: Optional[list] = None, json_schema: Optional[str] = None):
        self.session_id = session_id
        self.cwd = str(cwd)
        self.model = model
        self.permission_mode = permission_mode
        self.tools = tools or []
        self.mcp_servers = mcp_servers or []
        self.slash_commands = slash_commands or []
        self.include_partial_messages = include_partial_messages
        self.max_budget_usd = max_budget_usd
        self.json_schema = json_schema
        self.stream = stream or sys.stdout
        self._permission_denials = permission_denials if permission_denials is not None else []

        self._has_pending_assistant = False
        self._pending_text: list = []
        self._pending_thinking: list = []
        self._pending_tool_use: list = []
        self._pending_stop_reason = None
        self._pending_usage: dict = {}
        self._pending_tool_results: list = []

        self._final_text = ""
        self._usage: dict = {}
        self._total_cost_usd: Optional[float] = None
        self._num_turns = 0
        self._stop_reason: Optional[str] = None
        self._had_error = False
        self._error_message: Optional[str] = None
        self._budget_exceeded = False
        self._initted = False

    def _write(self, obj: dict) -> None:
        self.stream.write(json_module.dumps(obj) + "\n")
        self.stream.flush()

    def emit_init(self) -> None:
        """Idempotent: a caller driving several turns through ONE sink
        instance (`--input-format stream-json`) calls this once up front;
        `consume()` below only emits it itself when it hasn't happened
        yet, so reusing one sink across turns never repeats the `init`
        line (real Claude Code: one per SESSION, not one per turn)."""
        if self._initted:
            return
        self._initted = True
        self._write({
            "type": "system", "subtype": "init", "session_id": self.session_id, "cwd": self.cwd,
            "model": self.model, "permissionMode": self.permission_mode, "tools": self.tools,
            "mcp_servers": self.mcp_servers, "slash_commands": self.slash_commands,
            "rolo_claude_version": __import__("rolo_claude").__version__,
        })

    def _flush_pending(self) -> None:
        if self._has_pending_assistant:
            content = []
            thinking = "".join(self._pending_thinking)
            if thinking:
                content.append({"type": "thinking", "thinking": thinking})
            text = think_tag_strip("".join(self._pending_text))
            if text:
                content.append({"type": "text", "text": text})
            content.extend(self._pending_tool_use)
            self._write({
                "type": "assistant", "session_id": self.session_id,
                "message": {"role": "assistant", "model": self.model, "content": content,
                            "stop_reason": self._pending_stop_reason, "usage": self._pending_usage},
            })
            self._has_pending_assistant = False
            self._pending_text, self._pending_thinking, self._pending_tool_use = [], [], []
            self._pending_stop_reason, self._pending_usage = None, {}
        if self._pending_tool_results:
            self._write({
                "type": "user", "session_id": self.session_id,
                "message": {"role": "user", "content": list(self._pending_tool_results)},
            })
            self._pending_tool_results = []

    def _handle(self, event: ev.Event) -> None:
        kind = event.kind
        if kind == "message_start":
            self._flush_pending()
            self._has_pending_assistant = True
        elif kind == "text_delta":
            self._pending_text.append(event.data.get("text", ""))
            if self.include_partial_messages:
                self._write({"type": "stream_event", "session_id": self.session_id, "event": {
                    "type": "content_block_delta", "delta": {"type": "text_delta", "text": event.data.get("text", "")}}})
        elif kind == "thinking_delta":
            self._pending_thinking.append(event.data.get("text", ""))
            if self.include_partial_messages:
                self._write({"type": "stream_event", "session_id": self.session_id, "event": {
                    "type": "content_block_delta", "delta": {"type": "thinking_delta", "text": event.data.get("text", "")}}})
        elif kind == "tool_use_ready":
            self._pending_tool_use.append({"type": "tool_use", "id": event.data.get("id"),
                                            "name": event.data.get("name"), "input": event.data.get("input") or {}})
        elif kind == "tool_result":
            self._pending_tool_results.append({"type": "tool_result", "tool_use_id": event.data.get("id"),
                                                "content": event.data.get("summary", ""),
                                                "is_error": not event.data.get("ok", True)})
        elif kind == "message_end":
            self._pending_stop_reason = event.data.get("stop_reason")
            self._pending_usage = event.data.get("usage") or {}
            _merge_usage(self._usage, self._pending_usage)
            self._stop_reason = self._pending_stop_reason
            cost = event.data.get("cost_usd")
            if cost is not None:
                self._total_cost_usd = cost
                if self.max_budget_usd is not None and cost >= self.max_budget_usd:
                    self._budget_exceeded = True
        elif kind == "error":
            self._had_error = True
            self._error_message = event.data.get("message")
        elif kind == "turn_done":
            self._num_turns = event.turn
            self._final_text = think_tag_strip("".join(self._pending_text))
            self._flush_pending()

    def consume(self, event_iter: Iterator[ev.Event]) -> int:
        """Emits `init` only the first time (see `emit_init`), then drains
        `event_iter` for exactly one turn and prints its `result` line --
        safe to call again on the SAME sink for a later turn
        (`--input-format stream-json`): per-turn flags reset here so a
        prior turn's error/budget state never leaks into a later turn's
        otherwise-successful result, while `_usage`/`_num_turns` (session-
        cumulative) intentionally carry over."""
        self.emit_init()
        self._had_error = False
        self._error_message = None
        self._budget_exceeded = False
        for event in event_iter:
            self._handle(event)
            if self._budget_exceeded:
                event_iter.close()
                break
        return self.finish()

    def finish(self) -> int:
        if self._budget_exceeded:
            subtype, is_error = "error_max_budget_usd", True
        elif self._had_error:
            subtype, is_error = "error_during_execution", True
        else:
            subtype, is_error = "success", False
        result = build_result_object(
            session_id=self.session_id, model=self.model, num_turns=self._num_turns,
            stop_reason=self._stop_reason, usage=self._usage, total_cost_usd=self._total_cost_usd,
            result_text=self._error_message if self._had_error else self._final_text,
            is_error=is_error, subtype=subtype, permission_denials=self._permission_denials,
            structured_output=_try_structured_output(self._final_text, self.json_schema),
        )
        self._write(result)
        return 1 if is_error else 0

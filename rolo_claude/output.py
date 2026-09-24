"""rolo_claude.output -- print-mode ("headless") sinks. Plan D-TUI's
`headless.py` result-object shape, minimal H0 subset: `text` and `json`
only (`stream-json` is a later milestone -- U1 owns the full headless.py
with exit codes 0/1/2/130 and every flag; this is just enough for `-p`
to work end to end in H0).

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
) -> dict:
    """The `-p --output-format json` result object -- a subset of plan
    D-TUI's full shape (permission_denials/duration_ms/uuid are TUI/hook/
    permission concerns that don't exist yet in H0's tool-less loop)."""
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
        "model": model,
    }


class PrintModeSink:
    """Drains a Session.turn(...) event iterator and either prints plain
    text (the FINAL assistant message only, non-verbose; every message
    plus dimmed reasoning, verbose) or accumulates everything and prints
    one JSON result object at the end (cumulative usage/cost across every
    model call the turn made). Returns the process exit code (0 success,
    1 on an error event)."""

    def __init__(self, *, output_format: str = "text", session_id: str = "", model: str = "", stream=None,
                 verbose: bool = False):
        if output_format not in ("text", "json"):
            raise ValueError(f"unsupported output format: {output_format!r} (H0 supports text|json only)")
        self.output_format = output_format
        self.session_id = session_id
        self.model = model
        self.stream = stream or sys.stdout
        self.verbose = verbose
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
        for event in event_iter:
            self._handle(event)
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
        elif event.kind == "message_end":
            self._stop_reason = event.data.get("stop_reason")
            _merge_usage(self._usage, event.data.get("usage") or {})
            cost = event.data.get("cost_usd")
            if cost is not None:
                self._total_cost_usd = cost  # already the SESSION's cumulative total (agent/loop.py)
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
        if self.output_format == "text":
            if self._had_error:
                print(f"\nerror: {self._error_message}", file=sys.stderr)
            elif self._final_text:
                self.stream.write(self._final_text)
                if not self._final_text.endswith("\n"):
                    self.stream.write("\n")
                self.stream.flush()
        else:
            obj = build_result_object(
                session_id=self.session_id,
                model=self.model,
                num_turns=self._num_turns,
                stop_reason=self._stop_reason,
                usage=self._usage,
                total_cost_usd=self._total_cost_usd,
                result_text=self._final_text,
                is_error=self._had_error,
                subtype="error_during_execution" if self._had_error else "success",
            )
            print(json_module.dumps(obj))
        return 1 if self._had_error else 0

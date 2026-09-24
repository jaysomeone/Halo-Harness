"""rolo_claude.output -- print-mode ("headless") sinks. Plan D-TUI's
`headless.py` result-object shape, minimal H0 subset: `text` and `json`
only (`stream-json` is a later milestone -- U1 owns the full headless.py
with exit codes 0/1/2/130 and every flag; this is just enough for `-p`
to work end to end in H0).
"""

from __future__ import annotations

import json as json_module
import sys
from typing import Iterator, Optional

from rolo_claude import events as ev


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
    text (the assistant's text, streamed) or accumulates everything and
    prints one JSON result object at the end. Returns the process exit code
    (0 success, 1 on an error event)."""

    def __init__(self, *, output_format: str = "text", session_id: str = "", model: str = "", stream=None):
        if output_format not in ("text", "json"):
            raise ValueError(f"unsupported output format: {output_format!r} (H0 supports text|json only)")
        self.output_format = output_format
        self.session_id = session_id
        self.model = model
        self.stream = stream or sys.stdout
        self._text_parts: list = []
        self._had_error = False
        self._error_message: Optional[str] = None
        self._stop_reason: Optional[str] = None
        self._usage: dict = {}
        self._num_turns = 0

    def consume(self, event_iter: Iterator[ev.Event]) -> int:
        for event in event_iter:
            self._handle(event)
        return self.finish()

    def _handle(self, event: ev.Event) -> None:
        if event.kind == "text_delta":
            text = event.data.get("text", "")
            self._text_parts.append(text)
            if self.output_format == "text":
                self.stream.write(text)
                self.stream.flush()
        elif event.kind == "message_end":
            self._stop_reason = event.data.get("stop_reason")
            self._usage = event.data.get("usage") or {}
        elif event.kind == "error":
            self._had_error = True
            self._error_message = event.data.get("message")
        elif event.kind == "turn_done":
            self._num_turns = event.turn

    def finish(self) -> int:
        result_text = "".join(self._text_parts)
        if self.output_format == "text":
            if self._had_error:
                print(f"\nerror: {self._error_message}", file=sys.stderr)
            elif result_text and not result_text.endswith("\n"):
                self.stream.write("\n")
                self.stream.flush()
        else:
            obj = build_result_object(
                session_id=self.session_id,
                model=self.model,
                num_turns=self._num_turns,
                stop_reason=self._stop_reason,
                usage=self._usage,
                total_cost_usd=self._usage.get("cost") if isinstance(self._usage, dict) else None,
                result_text=result_text,
                is_error=self._had_error,
                subtype="error_during_execution" if self._had_error else "success",
            )
            print(json_module.dumps(obj))
        return 1 if self._had_error else 0

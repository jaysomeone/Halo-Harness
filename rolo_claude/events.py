"""rolo_claude.events -- the authoritative event/command contract between the
agent loop (rolo_claude.agent.loop.Session) and any UI. Print mode (H0) reads
these directly; the Textual TUI (U2+) will too. This module is pure data --
zero dependencies on the rest of the package -- so every layer can import it
without risking a cycle.

See the plan's D3 ("Message + event model") and D-Contract ("core <-> UI
reconciliation", which is authoritative where it adds to D3 -- the `status`
shape, `replay`, `notification`, and `parent_tool_use_id` on subagent events
all come from there). H0's own loop (no tools yet) only ever emits a subset
of these kinds (user_message, message_start, text_delta, thinking_delta,
message_end, status, error, turn_done) -- the rest of the vocabulary exists
now so H1+ milestones can start emitting them without changing this module's
contract shape.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# ---- kind vocabularies -----------------------------------------------------

EVENT_KINDS = frozenset({
    "user_message", "message_start", "text_delta", "thinking_delta",
    "tool_use_start", "tool_use_ready", "tool_progress", "tool_result",
    "permission_request", "question", "plan_review", "todos", "status",
    "message_end", "error", "turn_done", "subagent_start", "subagent_end",
    "replay", "notification",
})

COMMAND_KINDS = frozenset({
    "user_input", "interrupt", "set_mode", "set_model", "slash", "permission_reply",
    "question_reply", "plan_reply",
})

_ids = itertools.count(1)


def next_id(prefix: str) -> str:
    """A small monotonic id generator for request_id-shaped fields
    (permission_request, question, ...) -- not cryptographic, just unique
    within one process."""
    return f"{prefix}_{next(_ids)}"


@dataclass
class Event:
    """One item in the agent loop's outbound event stream. `data` holds the
    kind-specific payload as a plain dict (documented per-kind below); `turn`
    is the 1-based turn counter within the session, `agent_id` is None for
    the main session and a sub-agent's id when this event was produced by a
    (future) sub-agent run, and `ts` is a `time.time()` timestamp set at
    construction unless the caller supplies one (useful for replay/tests)."""
    kind: str
    data: dict = field(default_factory=dict)
    turn: int = 0
    agent_id: Optional[str] = None
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {self.kind!r} (expected one of {sorted(EVENT_KINDS)})")


@dataclass
class Command:
    """One item in the UI-to-loop command stream (Session.commands queue).
    `data` holds the kind-specific payload, documented per-kind below."""
    kind: str
    data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"unknown command kind: {self.kind!r} (expected one of {sorted(COMMAND_KINDS)})")


# ---- Event factory helpers --------------------------------------------------
# Thin constructors so agent/loop.py (and tests) don't hand-build `data`
# dicts inline everywhere; each documents its own payload shape. Kinds H1+
# will need (tool_use_start, permission_request, plan_review, ...) are left
# for the milestone that actually emits them to add alongside its own code,
# rather than guessing their exact payload shape here ahead of the tools
# that produce it.

def user_message(text: str, *, turn: int = 0, images: Optional[list] = None) -> Event:
    """data: {text, images}"""
    return Event("user_message", {"text": text, "images": images or []}, turn=turn)


def message_start(*, turn: int = 0, model: Optional[str] = None) -> Event:
    """data: {model}"""
    return Event("message_start", {"model": model}, turn=turn)


def text_delta(text: str, *, index: int = 0, turn: int = 0) -> Event:
    """data: {index, text}"""
    return Event("text_delta", {"index": index, "text": text}, turn=turn)


def thinking_delta(text: str, *, index: int = 0, turn: int = 0) -> Event:
    """data: {index, text}"""
    return Event("thinking_delta", {"index": index, "text": text}, turn=turn)


def message_end(*, turn: int = 0, stop_reason: Optional[str] = None, usage: Optional[dict] = None,
                 cost_usd: Optional[float] = None, context_pct: Optional[float] = None) -> Event:
    """data: {stop_reason, usage, cost_usd, context_pct}"""
    return Event("message_end", {
        "stop_reason": stop_reason, "usage": usage or {}, "cost_usd": cost_usd, "context_pct": context_pct,
    }, turn=turn)


def status(*, phase: str, model: Optional[str] = None, context_tokens: Optional[int] = None,
            context_limit: Optional[int] = None, cost_usd: Optional[float] = None, turn: int = 0,
            permission_mode: Optional[str] = None, mcp: Optional[dict] = None,
            session_id: Optional[str] = None) -> Event:
    """data: {phase, model, context_tokens, context_limit, cost_usd, turn,
    permission_mode, mcp: {connected, total}, session_id}. Emitted at
    session start, after every message_end, and on a mode/model change
    (D-Contract)."""
    return Event("status", {
        "phase": phase, "model": model, "context_tokens": context_tokens, "context_limit": context_limit,
        "cost_usd": cost_usd, "turn": turn, "permission_mode": permission_mode,
        "mcp": mcp or {"connected": 0, "total": 0}, "session_id": session_id,
    }, turn=turn)


def error(message: str, *, turn: int = 0, err_type: str = "error", retryable: bool = False,
          category: Optional[str] = None) -> Event:
    """data: {message, err_type, retryable, category}. `category` (H2 must-do
    3: providers.hooks.overflow_classifier) is the dsh-style taxonomy
    bucket (AUTH/RATE_LIMIT/CONTEXT_WINDOW_EXCEEDED/PROVIDER_FAILURE/
    MALFORMED_RESPONSE/EMPTY_RESPONSE) for a caller that wants to react by
    KIND of failure rather than parsing `err_type`'s per-source wire
    string; None when the caller didn't classify (e.g. a harness-internal
    error like tool_catalog_too_large that was never a wire error)."""
    return Event("error", {"message": message, "err_type": err_type, "retryable": retryable,
                            "category": category}, turn=turn)


def turn_done(*, turn: int = 0, reason: str = "end_turn") -> Event:
    """data: {reason} -- reason is one of "end_turn"|"max_turns"|"interrupted"|"error"."""
    return Event("turn_done", {"reason": reason}, turn=turn)


def notification(text: str, *, level: str = "info") -> Event:
    """data: {level, text}"""
    return Event("notification", {"level": level, "text": text})


def replay(messages: list) -> Event:
    """data: {messages} -- used by --resume to hand a UI the prior transcript."""
    return Event("replay", {"messages": messages})

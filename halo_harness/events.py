"""halo_harness.events -- the authoritative event/command contract between the
agent loop (halo_harness.agent.loop.Session) and any UI. Print mode (H0) reads
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
from typing import Optional

# ---- kind vocabularies -----------------------------------------------------

EVENT_KINDS = frozenset({
    "user_message", "message_start", "text_delta", "thinking_delta",
    "tool_use_start", "tool_use_ready", "tool_progress", "tool_result",
    "permission_request", "question", "plan_review", "todos", "status",
    "message_end", "error", "turn_done", "subagent_start", "subagent_end",
    "replay", "notification", "steer_queued", "steer_applied",
    "compaction",  # H5 scope B
    "phase", "steer_restart",  # Halo 2.0.1 W2a (liveness-tips-brief Part A6/GLM-brief item 3)
    "system_note",  # Halo 2.0.1 W5b: an async, out-of-band transcript line (e.g.
                     # background connector discovery finishing) -- pushed straight
                     # onto Controller.events from whatever thread noticed, never
                     # tied to an active turn's own generator.
})

COMMAND_KINDS = frozenset({
    "user_input", "interrupt", "set_mode", "set_model", "slash", "permission_reply",
    "question_reply", "plan_reply", "steer", "run_compact", "run_clear",
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
                 cost_usd: Optional[float] = None, context_pct: Optional[float] = None,
                 context_tokens: Optional[int] = None, context_limit: Optional[int] = None,
                 total_input_tokens: Optional[int] = None, total_output_tokens: Optional[int] = None) -> Event:
    """data: {stop_reason, usage, cost_usd, context_pct, context_tokens,
    context_limit, total_input_tokens, total_output_tokens}. The last four
    (1.0.1 hotfix 14) are the RAW numbers `context_pct` was already derived
    from, plus the session's running token totals -- added so a consumer
    (the TUI status bar) can render `"ctx 12k/1M 1%"`/`"in 12k out 3k"`
    without re-deriving anything itself; `context_pct` is kept for any
    existing consumer that only ever wanted the percentage."""
    return Event("message_end", {
        "stop_reason": stop_reason, "usage": usage or {}, "cost_usd": cost_usd, "context_pct": context_pct,
        "context_tokens": context_tokens, "context_limit": context_limit,
        "total_input_tokens": total_input_tokens, "total_output_tokens": total_output_tokens,
    }, turn=turn)


def status(*, phase: str, model: Optional[str] = None, context_tokens: Optional[int] = None,
            context_limit: Optional[int] = None, cost_usd: Optional[float] = None, turn: int = 0,
            permission_mode: Optional[str] = None, mcp: Optional[dict] = None,
            session_id: Optional[str] = None, total_input_tokens: Optional[int] = None,
            total_output_tokens: Optional[int] = None, effort: Optional[str] = None,
            subscription_usage: Optional[dict] = None) -> Event:
    """data: {phase, model, context_tokens, context_limit, cost_usd, turn,
    permission_mode, mcp: {connected, total}, session_id, total_input_tokens,
    total_output_tokens, effort, subscription_usage}. Emitted at session start, after every
    message_end, and on a mode/model change (D-Contract). The two token-
    total fields (1.0.1 hotfix 14) are the session's running input/output
    token counts, for a consumer (the status bar) to show `"in 12k out 3k"`
    when `cost_usd` is None (no price known for this model). `effort`
    (1.0.1 hotfix 20.3) is the session's current reasoning-effort level
    (`Session.effort`, already clamped to this route's own accepted set --
    see providers/profiles.py's `clamp_effort`), for the status bar's own
    short tag next to the mode glyph; None for a model with no adjustable
    effort at all. `subscription_usage` (`{session_pct, weekly_pct}`, see
    providers/sub_usage.py) is the account's 5 h / weekly usage for a `cc:`
    or `cx:` route; None for every other route or before the first reading."""
    return Event("status", {
        "phase": phase, "model": model, "context_tokens": context_tokens, "context_limit": context_limit,
        "cost_usd": cost_usd, "turn": turn, "permission_mode": permission_mode,
        "mcp": mcp or {"connected": 0, "total": 0}, "session_id": session_id,
        "total_input_tokens": total_input_tokens, "total_output_tokens": total_output_tokens, "effort": effort,
        "subscription_usage": subscription_usage,
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


def system_note(text: str) -> Event:
    """data: {text}. A plain transcript line with no turn/model context --
    unlike `notification` (a toast, `app.notify`), this renders as a real
    line in the scrolling transcript (`Transcript.add_note`), and unlike
    `user_message` it is never logged/sent to the model: purely informing
    whoever is watching the TUI that something happened in the background
    (Halo 2.0.1 W5b: "N claude.ai connectors available" once discovery
    finishes). Safe to push from ANY thread via `Controller.events.put(...)`
    at any time, including while no turn is active -- the 30 Hz drain loop
    (`tui/app.py::_drain`) is unconditional."""
    return Event("system_note", {"text": text})


def compaction(*, phase: str, trigger: str = "auto", turn: int = 0, tokens_before: Optional[int] = None,
               tokens_after: Optional[int] = None, headings_missing: Optional[list] = None,
               reason: Optional[str] = None) -> Event:
    """data: {phase, trigger, tokens_before, tokens_after, headings_missing,
    reason} (H5 scope B) -- `phase` is "start"|"retry"|"done"|"failed"|
    "skipped"; `trigger` is "manual"|"auto"|"overflow" (mirrors the
    `compacted` log node's own field and the PreCompact hook's `trigger`).
    Emitted by `Session._run_compaction` so a UI can show a "Compacting..."
    indicator and, on "done", how much room was freed. finding 4:
    `phase="failed"` (with a human-readable `reason`) means compaction was
    ATTEMPTED and gave up WITHOUT writing a `compacted` log node -- the
    history is unchanged. H5b finding 3: `phase="skipped"` (also a human-
    readable `reason`, also no log node written) means compaction was
    deliberately NOT attempted at all (the back-to-back auto-compaction
    guard) -- distinct from "failed" so a UI never renders a deliberate
    skip as an error."""
    return Event("compaction", {
        "phase": phase, "trigger": trigger, "tokens_before": tokens_before,
        "tokens_after": tokens_after, "headings_missing": headings_missing or [], "reason": reason,
    }, turn=turn)


def replay(messages: list) -> Event:
    """data: {messages} -- used by --resume to hand a UI the prior transcript."""
    return Event("replay", {"messages": messages})


def steer_queued(text: str, *, turn: int = 0) -> Event:
    """data: {text} -- scope 0(c): a steer was accepted (Controller.submit
    routed it to Session.steer because the session was busy) and is
    waiting for the next safe point (a chunk boundary mid-stream, or
    right after the current tool dispatch finishes) to actually apply."""
    return Event("steer_queued", {"text": text}, turn=turn)


def steer_applied(text: str, *, turn: int = 0) -> Event:
    """data: {text} -- the queued steer text was just appended as a
    user-role message and the loop is continuing with it."""
    return Event("steer_applied", {"text": text}, turn=turn)


# ---- Halo 2.0.1 W2a: phase events + steer_restart ---------------------
# HALO-2.0.1-liveness-tips-brief.md Part A1 names the live transcript line
# W2b renders from these; this module only defines the CONTRACT (when each
# fires, what it carries) -- agent/loop.py emits them, the UI (W2b) is the
# only thing that renders them. "Stall notices are computed in the UI from
# timestamps; no loop timer needed" (W2-plan item 6) -- this module and
# agent/loop.py never run a timer of their own for this.

PHASE_STATES = frozenset({"request_sent", "headers", "first_token", "waiting_for_model"})
PHASE_TOKEN_KINDS = frozenset({"reasoning", "text", "tool"})


def phase(*, state: str, turn: int = 0, model: Optional[str] = None, ttfb_ms: Optional[float] = None,
          kind: Optional[str] = None) -> Event:
    """data: {state, model, ttfb_ms, kind}. One per model-call transition,
    emitted by `agent/loop.py::Session._step` (and `_turn_body` for the
    last one), in this order per call:

      * `state="request_sent"` -- the request is about to go out; `model`
        is the model label the UI shows ("sending request to <model>...").
      * `state="headers"` -- the FIRST event of ANY kind arrived from the
        upstream generator (`message_start`, for both dialects -- see
        `_step`'s own comment on why that's effectively "response headers
        arrived" for the chat-dialect synthesized `message_start` too);
        `ttfb_ms` is the elapsed time since the request was sent.
      * `state="first_token", kind=...` -- the FIRST streamed content of
        ANY kind arrived: "reasoning" (a native `thinking` block opened, or
        -- for a chat-dialect route that never streams reasoning
        incrementally, e.g. Databricks GLM -- reasoning was found only once
        the stream finished), "text", or "tool". Fires exactly once per
        call, naming whichever kind got there first.
      * `state="waiting_for_model"` -- tool results were just dispatched
        back and the NEXT model call hasn't been sent yet (emitted by
        `_turn_body`, between steps of a multi-step tool-calling turn).

    A call with no streamed content at all (an immediate empty/error reply)
    may emit `request_sent`/`headers` with no `first_token` -- a UI must not
    assume all four always appear for every call."""
    return Event("phase", {"state": state, "model": model, "ttfb_ms": ttfb_ms, "kind": kind}, turn=turn)


def steer_restart(text: str, *, turn: int = 0) -> Event:
    """data: {text} -- GLM-brief.md item 3 / W2-plan item 2: a steer
    arrived while the in-flight model call had not produced any content
    yet (no chunk since the request was sent, config `steer.
    restart_when_silent`, default True) -- the call was aborted and is
    being resent with `text` appended, so no content is lost. W2b renders
    `↳ steering (restarting the model call)`; headless `--verbose` prints
    one line. Emitted once per restarted call, after the queued steer(s)
    were applied as a user-role message (the standard `steer_queued`/
    `steer_applied` pair fires too, exactly as an ordinary mid-stream
    steer's does) and before the retried request is built."""
    return Event("steer_restart", {"text": text}, turn=turn)

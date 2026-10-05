"""halo_harness.agent.escalation -- Halo 2.0.3 round 5e: hybrid escalation
as an explicit policy (`routing.escalation` in `~/.halo/config.json`).
Local first: a configured policy only ever matters on a local-model session
(`model.is_local_model_ref`) -- never on a cloud-model one (`Session.
_maybe_escalate`, agent/loop.py, checks this before anything here runs).

No new judging mechanism (brief's own words): the `low_confidence` trigger
reuses the EXACT role-resolution + one-shot-call shape `Session.
call_small_model` already established for the `small` role, just pointed at
the `judge` role instead (`roles.resolve_role_ref("judge", ...)` -- the
same role every decision-only/judge-endpoint routing in this codebase
already resolves through, see `roles.py`'s own `ROLE_NAMES` docstring);
`tool_failures` counts the current turn's own `is_error=True` tool_result
log nodes (no separate counter mechanism -- `count_tool_failures_since`
below is a pure read of the log the session already keeps); `context_
overflow` is the existing `providers.stream.ContextOverflow` path's own
per-turn counter (`Session._turn_context_overflow_count`, incremented at
the two call sites that already catch it).

This module holds the pure, Session-independent pieces (policy loading,
per-role override, the tool-failure count, the judge-reply parse, the
decision record shape) so each is unit-testable with no live Session/model
at all; the stateful "evaluate this turn, act on it" logic lives on
`Session` itself (agent/loop.py), since it needs the log, the role table,
`call_small_model`, and `set_model`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# The three triggers `routing.escalation.when` may name -- brief's own list,
# verbatim; anything else in a configured `when` list is dropped (never an
# error -- same "a bad entry just doesn't apply" tolerance every other
# config list in this codebase has).
TRIGGER_NAMES = ("low_confidence", "tool_failures", "context_overflow")

# Brief: "tool_failures>=2" -- the one trigger with a built-in threshold;
# the other two are boolean (did it happen at all this turn).
TOOL_FAILURE_THRESHOLD = 2


@dataclass(frozen=True)
class EscalationPolicy:
    to: str
    when: "tuple[str, ...]"
    ask: bool = True


def load_escalation_policy() -> "Optional[EscalationPolicy]":
    """`routing.escalation` -- `{to: <cloud ref>, when: [...], ask: bool}`.
    `None` when unset, not a dict, `to` missing/blank, or `when` has no
    recognized trigger name -- the caller then simply never evaluates any
    trigger, the same "absent means off" contract every other optional
    config knob in this codebase already uses. Never raises."""
    try:
        from halo_harness.theme import get_config_value
        raw = get_config_value("routing.escalation", default=None)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    to = raw.get("to")
    if not isinstance(to, str) or not to.strip():
        return None
    when_raw = raw.get("when")
    if isinstance(when_raw, str):
        when_raw = [when_raw]
    when = tuple(w for w in (when_raw or []) if isinstance(w, str) and w in TRIGGER_NAMES)
    if not when:
        return None
    ask_raw = raw.get("ask", True)
    ask = ask_raw if isinstance(ask_raw, bool) else True
    return EscalationPolicy(to=to.strip(), when=when, ask=ask)


def role_escalation_enabled(role_name: "Optional[str]", role_table: "Optional[dict]") -> bool:
    """Per-role override (brief: "Per-role overrides in the roles table may
    turn a role's escalation off") -- `role_table[role_name]` is a
    `{"model": ..., "escalation": false}` dict. Reads the RAW table entry
    directly rather than through `roles.role_value_parts` (which only ever
    unpacks `model`/`effort` -- a third key there would ripple into every
    other reader of a role-table value); `True` (escalation stays on) for
    everything else: no role name (the main/orchestrator session has none),
    no table, no entry for this role, a bare model-string entry, or an
    entry with no `escalation` key at all."""
    if not role_name or not isinstance(role_table, dict):
        return True
    entry = role_table.get(role_name)
    if not isinstance(entry, dict):
        return True
    return entry.get("escalation") is not False


def count_tool_failures_since(nodes: list, start_index: int) -> int:
    """How many `tool_result` nodes with `is_error=True` appear in `nodes`
    from `start_index` onward -- `nodes` is `SessionLog.nodes()`, `start_
    index` is `len(log.nodes())` captured at the top of the turn
    (`Session._turn_inner`). A pure list scan, no mutation, so it is safe
    to call from anywhere mid-turn, repeatedly, with no bookkeeping of its
    own to keep in sync."""
    count = 0
    for node in nodes[start_index:]:
        if isinstance(node, dict) and node.get("type") == "tool_result" and node.get("is_error"):
            count += 1
    return count


# The judge-role rubric call `Session._judge_confidence` (agent/loop.py)
# sends through `call_small_model` -- deliberately asks for ONE WORD so a
# small/local judge model (which may itself be the same model being judged,
# per `roles.resolve_role_ref`'s own "nothing configured -> the session's
# own model" fallback) has the best chance of answering in a parseable
# shape; `judge_says_confident` below tolerates surrounding punctuation/
# whitespace/case either way.
JUDGE_SYSTEM_PROMPT = (
    "You are judging a single assistant reply for confidence, not correctness. "
    "Reply with exactly one word: CONFIDENT if the reply directly and completely answers what was "
    "asked, or UNSURE if it hedges, asks for clarification instead of answering, admits it doesn't "
    "know, or is incomplete. One word only, no punctuation, no explanation."
)


def judge_says_confident(answer_text: "Optional[str]") -> bool:
    """`True` unless the judge's reply plainly says otherwise -- a judge
    call that failed, returned empty, or answered in some unparseable shape
    defaults to "confident" (benefit of the doubt, same house policy
    `providers.ollama_hw.vram_aware_override`'s own "fits, or unknown ->
    never guess, never block" rule already follows): a flaky judge call
    must never, by itself, force every local turn into an escalation."""
    if not answer_text:
        return True
    first_word = answer_text.strip().split()[0] if answer_text.strip() else ""
    normalized = first_word.strip(".,!:;\"'").upper()
    return normalized != "UNSURE"


@dataclass
class EscalationDecision:
    """One row of a session's escalation history (`/escalation`'s own
    "last decisions" list) -- session-local only (an in-memory list on
    `Session`, capped by the caller), never persisted across a restart;
    the brief asks `/escalation` to show "the last decisions", not a
    cross-session audit log."""
    turn: int
    trigger: str
    to: str
    action: str  # "asked" | "escalated"
    note: str = ""


def format_decision_line(d: "EscalationDecision") -> str:
    verb = "escalated to" if d.action == "escalated" else "asked about escalating to"
    return f"turn {d.turn}: {verb} {d.to} ({d.trigger})" + (f" -- {d.note}" if d.note else "")

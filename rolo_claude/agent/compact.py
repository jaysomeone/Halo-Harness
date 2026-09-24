"""rolo_claude.agent.compact -- H5 scope B: compaction mechanics as pure
functions (trigger math, the dsh-shaped 8-section replay request, tail
selection, knobs). The ORCHESTRATION -- actually issuing the summarisation
model call, appending the `compacted` log node + its replacement content,
and firing PreCompact/PostCompact/SessionStart(compact) -- is a method on
`agent.loop.Session` (`_run_compaction`), since only Session owns the
route/profile/creds/hook_runner a real model call needs; this module never
makes a network call or touches a SessionLog itself, which is what keeps it
trivially unit-testable.

Mechanism (dsh, reports/DeepSeek and OpenRouter ori harnesses.md): one
summarisation call whose PREFIX is the EXACT current derived request (system
node + tools + messages -- so the provider's own KV/prompt cache for this
session is reused, not invalidated) plus ONE final user message demanding
eight fixed Markdown sections; the reply is validated (all eight headings
present, `stop_reason` not a max-tokens finish) with one corrective retry;
the new transcript = a `<compacted-summary>` user node (merging any prior
summary -- see agent/derive.py's shadow-range note) + the last slice of
context verbatim, sized by OpenCode's tail-retention formula, never
splitting a reasoning+tool_use+tool_result unit, + re-injected snapshots +
a "files read this session" list.

Gate (OpenCode, Appendix D of reports/OpenCode harness deep review.md):
`usable = (limit.input or context - max_output) - reserved`, `reserved =
min(20_000, max_output)` -- adopted as the FLOOR the dsh-style 80% trigger
must never exceed (H5 brief scope F item 5): `min(dsh_trigger, usable)`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Trigger math
# ---------------------------------------------------------------------------

# dsh: "triggers at 80% of (context window - reserved output - 65,536
# headroom tokens)".
DSH_TRIGGER_PCT = 0.80
DSH_HEADROOM_TOKENS = 65_536

# OpenCode Appendix D: reserved = min(20_000, max_output).
OPENCODE_RESERVED_CAP = 20_000

# OpenCode Appendix D tail-retention formula (adopted per H5 scope F item 5,
# REPLACING dsh's flat "retain 16%"): min(15k, max(2k, 25% of usable)).
TAIL_MIN_TOKENS = 2_000
TAIL_MAX_TOKENS = 15_000
TAIL_PCT_OF_USABLE = 0.25

CHARS_PER_TOKEN = 4


def _estimate_tokens(obj) -> int:
    import json
    try:
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(obj)
    return max(0, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def opencode_usable(context_tokens: int, max_output_tokens: int, *, input_limit: Optional[int] = None) -> int:
    """`usable = (limit.input or context - max_output) - reserved`,
    `reserved = min(20_000, max_output)` -- OpenCode Appendix D, verbatim.
    `input_limit` is this profile's own separately-reported INPUT limit when
    a provider publishes one distinct from the combined context window
    (rolo-claude's `ModelProfile` doesn't distinguish them today, so callers
    normally omit it and get `context_tokens - max_output_tokens`)."""
    reserved = min(OPENCODE_RESERVED_CAP, max(0, max_output_tokens))
    base = input_limit if input_limit else max(0, context_tokens - max_output_tokens)
    return max(0, base - reserved)


def dsh_trigger_tokens(context_tokens: int, max_output_tokens: int, *, pct: float = DSH_TRIGGER_PCT) -> int:
    """dsh's own 80%-of-headroom trigger, before the OpenCode floor is
    applied. Clamped at 0 (a tiny `context_tokens` override -- e.g. a test's
    `CLAUDE_CODE_AUTO_COMPACT_WINDOW=20000` against the 65,536 headroom
    constant -- legitimately drives this negative, meaning "compact on the
    very next usage update", which is exactly the desired fast-forcing
    behaviour for that knob)."""
    return max(0, int(pct * (context_tokens - max_output_tokens - DSH_HEADROOM_TOKENS)))


def compaction_trigger_tokens(context_tokens: int, max_output_tokens: int, *, pct_override: Optional[int] = None) -> int:
    """The number of prompt tokens at/above which compaction should fire:
    `min(dsh_trigger_tokens(...), opencode_usable(...))` -- OpenCode's gate
    is the floor the 80% rule must never exceed (H5 brief scope F item 5).
    `pct_override` is `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (1-100, "can only
    lower" -- D-CFG): only ever LOWERS the effective percentage below 80%,
    never raises it."""
    pct = DSH_TRIGGER_PCT
    if isinstance(pct_override, int) and 1 <= pct_override <= 100:
        pct = min(pct, pct_override / 100.0)
    dsh = dsh_trigger_tokens(context_tokens, max_output_tokens, pct=pct)
    return min(dsh, opencode_usable(context_tokens, max_output_tokens))


def tail_retention_tokens(usable: int) -> int:
    return min(TAIL_MAX_TOKENS, max(TAIL_MIN_TOKENS, int(usable * TAIL_PCT_OF_USABLE)))


# ---------------------------------------------------------------------------
# Knobs (D-CFG "Compaction" bullet + brief scope B)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompactionKnobs:
    disabled: bool = False                       # DISABLE_COMPACT env
    auto_compact_enabled: bool = True             # settings.json autoCompactEnabled
    window_override: Optional[int] = None         # CLAUDE_CODE_AUTO_COMPACT_WINDOW env > settings.autoCompactWindow
    pct_override: Optional[int] = None            # CLAUDE_AUTOCOMPACT_PCT_OVERRIDE env, 1-100, can only lower
    compaction_model: Optional[str] = None        # settings/config compactionModel (default: the session's main model)


def _truthy_env(value: Optional[str]) -> bool:
    return value is not None and value.strip().lower() not in ("", "0", "false", "no")


def resolve_knobs(settings, env: Optional[dict] = None) -> CompactionKnobs:
    """`env` defaults to `os.environ`; a caller resolving from
    `settings.effective_env` (must-do 6's real precedence chain) passes that
    dict explicitly instead. `CLAUDE_CODE_AUTO_COMPACT_WINDOW` "wins over
    everything" (D-CFG) -- checked before `settings.autoCompactWindow`."""
    env = env if env is not None else os.environ
    disabled = _truthy_env(env.get("DISABLE_COMPACT"))

    window_override = None
    raw_window = env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    if raw_window is not None:
        try:
            window_override = int(raw_window)
        except (TypeError, ValueError):
            window_override = None
    if window_override is None and settings is not None:
        setting_window = getattr(settings, "auto_compact_window", None)
        if isinstance(setting_window, int):
            window_override = setting_window
        elif isinstance(setting_window, str) and setting_window.strip().lower() not in ("", "auto"):
            try:
                window_override = int(setting_window)
            except ValueError:
                window_override = None

    pct_override = None
    raw_pct = env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    if raw_pct is not None:
        try:
            pct_override = max(1, min(100, int(raw_pct)))
        except (TypeError, ValueError):
            pct_override = None

    auto_enabled = True
    if settings is not None:
        auto_enabled = bool(getattr(settings, "auto_compact_enabled", True))

    compaction_model = None
    if settings is not None:
        compaction_model = getattr(settings, "compaction_model", None)
        if not compaction_model:
            raw = getattr(settings, "raw", None)
            if isinstance(raw, dict):
                compaction_model = raw.get("compactionModel")

    return CompactionKnobs(disabled=disabled, auto_compact_enabled=auto_enabled,
                            window_override=window_override, pct_override=pct_override,
                            compaction_model=compaction_model)


def should_compact(prompt_tokens: int, context_tokens: int, max_output_tokens: int, knobs: CompactionKnobs) -> "tuple[bool, int]":
    """Returns (should_compact, trigger_tokens). `DISABLE_COMPACT` and
    `autoCompactEnabled=false` both mean "never" (the caller still honours a
    manual `/compact` or a hard `ContextOverflow` regardless -- those don't
    call this gate at all, see Session._run_compaction)."""
    if knobs.disabled or not knobs.auto_compact_enabled:
        return False, 0
    effective_context = knobs.window_override if isinstance(knobs.window_override, int) and knobs.window_override > 0 else context_tokens
    trigger = compaction_trigger_tokens(effective_context, max_output_tokens, pct_override=knobs.pct_override)
    return prompt_tokens >= trigger, trigger


# ---------------------------------------------------------------------------
# The 8-section checkpoint (dsh) -- exact heading names from the H5 brief /
# reports/DeepSeek and OpenRouter ori harnesses.md ("Compaction is the part
# of dsh most worth copying wholesale" paragraph).
# ---------------------------------------------------------------------------

SUMMARY_HEADINGS = (
    "Primary Request and Intent", "Key Technical Concepts", "Files and Code",
    "Errors and Fixes", "Pending Jobs", "Current Work", "Next Step", "Critical Context",
)

# dsh: "forbids mentioning the compaction, treats a max-tokens finish as
# failure, and merges any prior <compacted-summary> rather than copying it
# forward". The instruction prose itself is this module's own (dsh's exact
# summariser prose is not quoted in the source reports beyond these rules
# and the section names, which ARE used verbatim above).
_INSTRUCTION_TEMPLATE = """Your task is to create a detailed summary of the conversation above, in \
preparation for compacting it to free up context. This summary will REPLACE everything above it, so \
it must capture everything another agent would need to continue this work with no other context.

Produce the summary as Markdown with EXACTLY these eight `##` headings, in this order, every one \
present even when a section has nothing to report (write "(none)" rather than omitting it):

## Primary Request and Intent
## Key Technical Concepts
## Files and Code
## Errors and Fixes
## Pending Jobs
## Current Work
## Next Step
## Critical Context

Rules: preserve exact file paths, symbols, commands, error strings, URLs, and identifiers whenever \
they appear above -- never paraphrase them away. If a prior <compacted-summary> block appears above, \
merge its still-relevant content into the new summary (carry forward objectives, constraints, \
decisions and parallel workstreams it recorded) rather than omitting or re-quoting it verbatim -- the \
conversation above wins wherever the two conflict. Do not mention this summarization process, that \
context was compacted, or that you were asked to produce this summary -- write only the eight \
sections themselves."""

_CORRECTIVE_SUFFIX = """

Your previous attempt was incomplete: {problem}. Produce the COMPLETE checkpoint again, with all \
eight headings above present in order (write "(none)" for an empty section rather than skipping it); \
be more concise in each section if that is what it takes to fit."""


def build_summary_instruction(custom_instructions: Optional[str] = None, *, corrective: bool = False,
                               problem: str = "some required sections were missing or the reply was cut off") -> str:
    """The ONE final user message appended after the exact replayed prefix
    (scope B). `custom_instructions` is `/compact [instructions]`'s own free
    text (e.g. "focus on file names"), appended as extra guidance -- never
    replacing the eight required headings."""
    text = _INSTRUCTION_TEMPLATE
    if custom_instructions and custom_instructions.strip():
        text += f"\n\nAdditional focus requested by the user: {custom_instructions.strip()}"
    if corrective:
        text += _CORRECTIVE_SUFFIX.format(problem=problem)
    return text


def validate_summary(text: str, stop_reason: Optional[str]) -> "tuple[bool, list]":
    """(ok, missing_headings). A `max_tokens`/`length` finish is ALWAYS a
    failure regardless of heading content (dsh: "treats a max-tokens finish
    as failure") -- the reply may look complete but was truncated by the
    provider, not the model choosing to stop."""
    if stop_reason in ("max_tokens", "length"):
        return False, list(SUMMARY_HEADINGS)
    text = text or ""
    missing = [h for h in SUMMARY_HEADINGS if f"## {h}" not in text]
    return (not missing), missing


REPLACEMENT_PREAMBLE = ("This session was compacted to save context. Continue the task directly from "
                         "the summary below without acknowledging this checkpoint.")


def wrap_compacted_summary(summary_text: str) -> str:
    return f"{REPLACEMENT_PREAMBLE}\n\n<compacted-summary>\n{summary_text.strip()}\n</compacted-summary>"


def find_prior_summary(messages: list) -> Optional[str]:
    """Best-effort scan for an existing `<compacted-summary>` block anywhere
    in the CURRENT derived messages (present only when this session was
    already compacted once before) -- used only for a human-readable log/
    event field; the merge itself happens because the replay prefix already
    includes this text (see agent/derive.py), not because this function's
    return value is threaded back into the request."""
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str) and "<compacted-summary>" in text:
                return text
    return None


# ---------------------------------------------------------------------------
# Verbatim tail selection -- never splits a reasoning + tool_use + tool_result
# unit (scope B).
# ---------------------------------------------------------------------------

def _message_tokens(msg: dict) -> int:
    return _estimate_tokens(msg)


def _is_tool_result_message(msg: dict) -> bool:
    content = msg.get("content") if isinstance(msg, dict) else None
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def select_verbatim_tail(messages: list, tail_tokens: int) -> list:
    """The last slice of `messages` whose estimated size fits within
    `tail_tokens`, walked from the END backward one ATOMIC UNIT at a time.
    A unit is normally one message; but an assistant message that contains
    any `tool_use` block is glued to the tool_result message that must
    immediately follow it (Anthropic's own wire pairing requirement -- and
    exactly the "reasoning + tool_use + tool_result... atomic unit" rule the
    brief names), so the tail can never start mid-pair. Always keeps at
    LEAST the last unit even if it alone exceeds `tail_tokens` (an empty
    tail is never useful)."""
    if not messages:
        return []
    units: list = []
    i = len(messages) - 1
    while i >= 0:
        msg = messages[i]
        if _is_tool_result_message(msg) and i > 0 and messages[i - 1].get("role") == "assistant":
            units.append([messages[i - 1], messages[i]])
            i -= 2
        else:
            units.append([msg])
            i -= 1
    # `units` is newest-first; accumulate until the budget is exceeded.
    kept: list = []
    total = 0
    for unit in units:
        unit_tokens = sum(_message_tokens(m) for m in unit)
        if kept and total + unit_tokens > tail_tokens:
            break
        kept.append(unit)
        total += unit_tokens
    kept.reverse()
    return [m for unit in kept for m in unit]


def build_files_read_snapshot(paths: list) -> Optional[str]:
    if not paths:
        return None
    lines = "\n".join(f"- {p}" for p in sorted(paths))
    return f"# Files read this session\n\n{lines}"

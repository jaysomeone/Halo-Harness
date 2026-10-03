"""halo_harness.agent.loop -- the agent loop (H1 rewrite, scope E-H; H2
must-do 3-6 + findings 1-16 layered on top).

Every model request is DERIVED from the append-only SessionLog
(agent/log.py + agent/derive.py) -- `self.messages` (H0's ad-hoc
in-memory list) is gone. Real tool dispatch (tools/registry.py), the loop
breaker (rule 8), serialize-time invariants on interrupt/error
(agent/invariants.py), canonical Anthropic block accumulation (accumulate
`partial_json`, decode once at content_block_stop -- finding 12), and
per-profile reasoning replay via providers/request.py + providers/hooks.py.

H2: `_derive_and_build` derives tools from the logged FROZEN catalog
(finding 4), `_step` hashes the EXACT body sent (finding 4), catches
`ToolCatalogTooLarge` (must-do 4), retries on a real 1-2-4-8-16s/max-5
ladder with an abort-aware wait (finding 7), retries one empty completion
before failing (finding 7), treats `length_with_minimal_output` as a
provider failure (finding 3), and passes `Session.abort` into
`stream_completion` so a future interrupt source can cut phase 2 (and,
via `providers/stream.py`'s connect-time watcher, phase 1) short
(must-do 5). `_turn_body` counts MODEL CALLS (not `turn()` calls) against
`--max-turns` (finding 9) and gives a length-truncated/malformed tool call
its own `is_error` result instead of silently ending the turn
(finding 3).
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import logging
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator, Optional

from halo_harness import events
from halo_harness.config.paths import env_compat
from halo_harness.agent.planmode import PLAN_MODE_NOTE, ensure_plan_file, write_plan
from halo_harness.agent.jobs import JobRegistry
from halo_harness.agent.subagent import AgentRuntime
from halo_harness.agent.compact import (
    CHARS_PER_TOKEN, build_files_read_snapshot, build_summary_instruction, resolve_knobs, select_verbatim_tail,
    should_compact, tail_retention_tokens, validate_summary, wrap_compacted_summary,
)
from halo_harness.agent.compact import opencode_usable as _opencode_usable
from halo_harness.agent.derive import content_hash_from_oai_body, derive_request
from halo_harness.agent.invariants import repair_truncated_text, synthesize_missing_results, validate_tool_use
from halo_harness.agent.log import SessionLog
from halo_harness.agent.prune import PRUNE_PROTECT_TOKENS, PRUNE_REBALANCE_CHUNK_TOKENS, compute_stub_candidates, prune_messages
from halo_harness.agent.repair import build_tool_meta, repair_assistant_turn
from halo_harness.hooks import HookRunner
from halo_harness.model import CostMeter, ModelProfile, ModelRef, parse_model_ref, resolve_model_profile
from halo_harness.permissions import Decision, PermissionEngine
from halo_harness.providers.errors import (
    CONTEXT_WINDOW_EXCEEDED, MAX_RETRIES, is_effort_rejected_message, is_effort_with_tools_rejected_message,
    is_reasoning_replay_bug, is_retryable_message, retry_delay_ms,
)
from halo_harness.providers.http import is_connect_failure_message
from halo_harness.providers.hooks import (
    classify_length_tool_call, is_retryable_empty_completion, leak_parser,
    max_tokens_budget, overflow_classifier, record_databricks_output_tokens,
)
from halo_harness.providers.config import tool_child_env
from halo_harness.providers.profiles import ProviderProfile, resolve_profile
from halo_harness.providers.request import ToolCatalogTooLarge, build_anthropic_request_body, build_request_body
from halo_harness.providers.routing import InvalidModelError, Route
from halo_harness.providers.stream import (
    CompletionRequest, ContextOverflow, ProviderCreds, ProviderNotConfigured,
    UpstreamError, stream_anthropic_completion, stream_completion,
)
from halo_harness.tools.base import ToolContext, ToolResult
from halo_harness.tools.imageutil import sniff_dimensions
from halo_harness.tools.registry import ToolRegistry, run_read_only_batch
from halo_harness.tools.truncate import spill_and_truncate

log = logging.getLogger("bridge")

_MAX_RETRY_WAIT_S = 300.0  # H8 cheap must-do: raised from 60s -- a provider's own longer
# Retry-After (a real 429 body can legitimately ask for several minutes) is now honoured up
# to 5 minutes instead of being silently clipped to one; still never the fully-uncapped
# `time.sleep(Retry-After)` H0 had (that could be RETRY_MAX_DELAY_MS, effectively forever).

# H5 scope F item 9 (OpenCode Appendix H, verbatim): injected as the FINAL
# user-role text when `--max-turns`/max_turns is exhausted, so the model
# closes with a real summary instead of dying mid-tool-loop with no chance
# to respond. `_turn_body`'s max_turns branch appends this as a snapshot
# and runs ONE more (tool-less) `_step` before ending the turn.
MAX_STEPS_PROMPT = """CRITICAL - MAXIMUM STEPS REACHED

The maximum number of steps allowed for this task has been reached. Tools are disabled until next user input. Respond with text only.

STRICT REQUIREMENTS:
1. Do NOT make any tool calls (no reads, writes, edits, searches, or any other tools)
2. MUST provide a text response summarizing work done so far
3. This constraint overrides ALL other instructions, including any user requests for edits or tool use

Response must include:
- Statement that maximum steps for this agent have been reached
- Summary of what has been accomplished so far
- List of any remaining tasks that were not completed
- Recommendations for what should be done next

Any attempt to use tools is a critical violation. Respond with text ONLY."""

# H5 scope B: a distinguishable return value from `_step` (never a real
# `_StepResult`, never plain `None`) meaning "phase 1 raised ContextOverflow
# and no compaction has been attempted yet for THIS model call" --
# `_turn_body` reacts by running `_run_compaction` once and re-calling
# `_step(overflow_handled=True)`; a SECOND overflow on that retry falls
# through to `_step`'s own ordinary terminal-error branch instead of
# looping forever.
_OVERFLOW_NEEDS_COMPACTION = object()

_LOOP_BREAKER_REMIND_AT = 3
_LOOP_BREAKER_DENY_AT = 5
_LOOP_BREAKER_END_AT = 8

# A wire ("error" SSE event) carries an Anthropic-shaped `type` string, not
# an HTTP status -- reverses providers.errors.map_upstream_error's own
# status->type table so hooks.overflow_classifier (which wants a status)
# can still classify a wire error by the SAME taxonomy as a phase-1 one.
_WIRE_ERROR_TYPE_TO_STATUS = {
    "authentication_error": 401, "permission_error": 403, "not_found_error": 404,
    "invalid_request_error": 400, "rate_limit_error": 429, "api_error": 500,
    "overloaded_error": 503,
}


def _capped_retry_delay(attempts: int, hdrs: dict) -> float:
    """`retry_delay_ms` (Appendix B) capped at `_MAX_RETRY_WAIT_S` (H8
    cheap must-do: 300s, raised from 60s -- log it whenever a provider's
    own longer `Retry-After` actually gets clipped, so a wait that looks
    short in the log is never silently hiding a provider that asked for
    much longer)."""
    raw_s = retry_delay_ms(attempts, hdrs) / 1000.0
    if raw_s > _MAX_RETRY_WAIT_S:
        log.warning("retry wait capped at %.0fs (provider asked for %.0fs)", _MAX_RETRY_WAIT_S, raw_s)
    return min(raw_s, _MAX_RETRY_WAIT_S)


def _mcp_blocks_for_log(blocks: list, *, meta, session_dir, tool_use_id) -> list:
    """finding 5: log a block-content tool result (MCP tools; any future
    tool that returns Anthropic-shaped content blocks) AS BLOCKS, never
    pre-flattened to text -- a vision image stays a REAL image block so
    `providers/request.py`'s own "image hoisting" step can find and
    surface it to the model, and a `tool_reference` block (ToolSearch's
    own load-confirmation marker -- never something a TOOL's result
    itself should carry) is dropped rather than rendered as
    "[tool_reference block]". Capping happens HERE, in the finalize step
    -- ONE canonical pass, via `cap_and_spill` (mcp_tool.py's own
    implementation, reused rather than duplicated), running AFTER the
    tool itself ran so a future H4 PostToolUse hook sees the model's
    real, full output. (The old `_mcp_content_to_text` flattened to text
    first and then cut AGAIN at a hard-coded, env-var-blind 25000 tokens,
    so Claude Code's own truncation string never reached the model
    intact and no spill path was ever named -- both fixed by having
    exactly one capping pass, here.)"""
    from halo_harness.tools.mcp_tool import cap_and_spill
    filtered = [b for b in blocks if not (isinstance(b, dict) and b.get("type") == "tool_reference")]
    if not filtered:
        filtered = [{"type": "text", "text": "(no content returned)"}]
    return cap_and_spill(filtered, meta=meta, session_dir=session_dir, tool_use_id=tool_use_id)


_SPILL_MARKER = "Full output saved to "  # tools/truncate.py + tools/mcp_tool.py's own exact wording


def _classify_tool_error_text(name: str, text: str) -> str:
    """H10 Part A: `tool_result.error_class` for an `is_error=True` result
    that actually reached a tool (a repair/permission/loop-breaker
    rejection is classified structurally instead, at `_resolve_tool_call`'s
    own call sites -- this function only ever sees a REAL tool's own
    message). Pattern-matched against strings THIS codebase's own tools
    author (tools/edit.py, tools/read.py, ...), never model or file
    content, so this is telemetry categorization, not a content classifier
    over anything a model or the user wrote."""
    if "has not been read yet" in text or "Read it again before editing" in text:
        return "read_before_edit"
    if "Found multiple matches" in text:
        return "multiple_matches"
    if "not found" in text or "does not exist" in text:
        return "not_found"
    if "timed out" in text or "timeout" in text.lower():
        return "timeout"
    if name.startswith("mcp__"):
        return "mcp_error"
    return "other"


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} MB"


def _image_caption(block: dict) -> str:
    """H8 scope B ("screenshots ... shown as cards"): a real image block's
    display caption -- media type, dimensions (sniffed from the raw bytes,
    same pure-stdlib sniffer `tools/imageutil.py` uses elsewhere, never
    requires Pillow) and a human byte size, e.g. "[image: image/png,
    1280x800, 84.2 KB]" -- this is the `tool_result` EVENT's own display-
    only `summary` text (never what's sent to the model, see
    `_summary_text_for_blocks`), and the TUI's fallback display: real
    inline terminal rendering (the kitty graphics protocol,
    WezTerm/Ghostty/foot, or sixel, live-detected, downscaled) is handled
    separately by `tui/images.py`/`ToolCard` (H13 Part B) and shown INSTEAD
    of this caption whenever the terminal supports it and `images` isn't
    set to `"caption"`/`"off"` -- this text is what appears everywhere else
    (no graphics protocol, `images: "caption"`/`--no-inline-images`, or a
    render attempt that itself fails), replacing the old bare "[image]"
    placeholder with an honest, concrete description of what was captured.
    Degrades all the way back to the plain "[image]" this
    replaces when the block truly carries nothing describable (no
    media_type, no decodable data -- e.g. a hand-built `{"type": "image",
    "source": {}}` in a test)."""
    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    media_type = source.get("media_type") if isinstance(source, dict) else None
    data = source.get("data") if isinstance(source, dict) else None
    raw = None
    if isinstance(data, str):
        try:
            raw = base64.b64decode(data, validate=False)
        except (binascii.Error, ValueError):
            raw = None
    dims = sniff_dimensions(raw) if raw else None
    parts = []
    if media_type:
        parts.append(media_type)
    if dims:
        parts.append(f"{dims[0]}x{dims[1]}")
    if raw is not None:
        parts.append(_human_bytes(len(raw)))
    return f"[image: {', '.join(parts)}]" if parts else "[image]"


def _summary_text_for_blocks(blocks: list) -> str:
    """A short, human-readable stand-in for the `tool_result` EVENT's
    `summary` field (display/verbose-log only -- never what's logged to
    the session or sent to the model, which is the full block list) --
    the first real text block; an all-image result (a Playwright/Chrome
    screenshot, a vision Read/MCP image result) gets one `_image_caption`
    line per image; anything else (or a mix) gets an honest placeholder
    naming whatever content kind(s) came back (e.g. "[document]")."""
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
            return b["text"]
    image_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "image"]
    if image_blocks and len(image_blocks) == len(blocks):
        return "\n".join(_image_caption(b) for b in image_blocks)
    kinds = sorted({b.get("type", "content") for b in blocks if isinstance(b, dict)}) or ["content"]
    return f"[{', '.join(kinds)}]"


def _canonical_args(args) -> str:
    """Canonical JSON (sorted keys) for loop-breaker hashing -- property
    order must not matter (rule 8: "same tool name + canonically-equal
    arguments, property order ignored")."""
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


def _reasoning_details_display_text(details) -> str:
    """Best-effort human text from an OpenRouter `reasoning_details[]`
    array (finding 15's --verbose thinking display, for the case where a
    model's ONLY reasoning signal is the structured array -- no plain
    top-level `reasoning`/`reasoning_content` string alongside it)."""
    if not isinstance(details, list):
        return ""
    parts = []
    for entry in details:
        if isinstance(entry, dict):
            text = entry.get("text") or entry.get("summary")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


# H2 scope B: the tool_choice=required retry must NEVER fire on an
# ordinary, legitimate final answer (the overwhelmingly common case when a
# tool call is absent -- the model is simply DONE) -- only on a genuine
# signal the model ATTEMPTED a text-embedded call whose full leak_parser
# regex didn't cleanly match (truncated/malformed markup). Plain substring
# checks, not `leak_parser`'s own precise regexes, so a partially-formed
# or slightly-off tag still counts as "attempted" even though it couldn't
# be parsed into a usable call.
_LEAK_ATTEMPT_MARKERS = ("<tool_call", "<｜DSML｜", "<|tool_call", "<minimax:tool_call", "<function=")
# finding 2: a bare substring match ANYWHERE used to fire this (Kimi bug:
# an ordinary answer that merely MENTIONS `<tool_call>` while discussing
# tool-calling, with normal prose continuing well past it, always
# triggered a forced tool_choice=required retry and discarded a perfectly
# good answer) -- the LAST marker occurrence must instead be near the very
# END of the text (the model was cut off mid-attempt), never just present
# somewhere in a longer message.
_LEAK_ATTEMPT_TAIL_CHARS = 80


def _looks_like_attempted_tool_call(text: str) -> bool:
    if not text:
        return False
    last_pos = -1
    for marker in _LEAK_ATTEMPT_MARKERS:
        idx = text.rfind(marker)
        if idx > last_pos:
            last_pos = idx
    if last_pos == -1:
        return False
    return (len(text) - last_pos) <= _LEAK_ATTEMPT_TAIL_CHARS


# finding 2: markers that can open a promoted-from-text leak shape, used
# ONLY to find where to truncate the LOGGED text (never for detection --
# leak_parser itself already decided a promotion happened).
_LEAK_STRIP_MARKERS = _LEAK_ATTEMPT_MARKERS + ("```json", "```", "<tool_calls>")


def _strip_promoted_leak_text(text: str) -> str:
    """Truncate `text` at the earliest point any leak-shaped marker
    begins, keeping only the (typically short) prefix before it -- see
    the call site for why this must happen once a call is promoted."""
    if not text:
        return text
    earliest = len(text)
    for marker in _LEAK_STRIP_MARKERS:
        idx = text.find(marker)
        if idx != -1 and idx < earliest:
            earliest = idx
    return text[:earliest].rstrip()


def _rough_estimate(system_text: str, messages: list) -> int:
    """len(json)/4, matching providers.config.estimate_tokens' own rule of
    thumb -- used only for the max_tokens budget headroom calculation."""
    try:
        blob = json.dumps({"system": system_text, "messages": messages}, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = str(messages)
    return max(1, len(blob) // 4)


def _total_prompt_tokens(usage: Optional[dict]) -> Optional[int]:
    """H5b finding 4: `usage.input_tokens` ALONE understates a cached
    native-Claude-route reply's real prompt size -- on `ant:`/Databricks
    Claude passthrough, `message_start` reports only the UNCACHED portion,
    excluding `cache_read_input_tokens`/`cache_creation_input_tokens`
    (verified: a 150,000-token cached prompt reported `input_tokens=40`).
    Every place a prompt's real size drives a decision or a display --
    the compaction trigger (`_account_usage`), `context_pct` -- must use
    input + cache_read + cache_creation, not `input_tokens` alone. Returns
    None only when `usage` itself carries no usable `input_tokens` at all
    (an OpenAI-dialect reply with no cache fields just adds two zeros)."""
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    if not isinstance(input_tokens, int):
        return None
    total = input_tokens
    cache_read = usage.get("cache_read_input_tokens")
    if isinstance(cache_read, int):
        total += cache_read
    cache_creation = usage.get("cache_creation_input_tokens")
    if isinstance(cache_creation, int):
        total += cache_creation
    return total


# finding 4: OpenCode's own fallback shape (reports/OpenCode harness deep
# review.md Appendix D) for when the EXACT structured prefix a normal
# summarisation call replays is itself too big to send (a real
# `ContextOverflow` on the summariser call, not just the ordinary "trigger
# auto-compaction" case -- this only ever runs as a SECOND attempt, after
# the structured replay already failed once). Every message is flattened to
# plain text with no `tools` offered, which is both far smaller (no JSON
# schema/tool-call structure) and, unlike the structured replay, immune to
# the "history itself contains an unresolved tool_use/tool_result pairing"
# shape rules a provider might otherwise reject.
_SUMMARY_TOOL_RESULT_CHARS = 2000

# H5c finding 22: the OLD `_serialize_transcript_for_summary` flattened the
# WHOLE (already-pruned) transcript with no bound on the TOTAL result size --
# each individual tool_result was capped at `_SUMMARY_TOOL_RESULT_CHARS`, but
# a transcript with hundreds of turns/short results could still overflow the
# fallback call's own (small) context window a second time. Bounded to a
# SHARE of the model's context window (never a flat constant, so a huge-
# context model still gets a generous fallback), between a floor that is
# never useless and an absolute ceiling that keeps the fallback body itself
# reasonably sized regardless of context window. "Serialise only the HEAD" --
# this is a last-resort emergency fallback with no further retry, so the
# earliest content (the original request/intent, most likely to matter to a
# summary) is kept and any excess tail is dropped, never the reverse.
_SUMMARY_FALLBACK_HEAD_SHARE = 0.5
_SUMMARY_FALLBACK_MIN_CHARS = 20_000
_SUMMARY_FALLBACK_MAX_CHARS = 400_000
_SUMMARY_FALLBACK_TRUNCATED_MARKER = "\n\n[... transcript truncated to fit the context window ...]"

# H5c finding 22: after a FAILED auto-compaction attempt (exhausted the
# summarisation call's own retry ladder, or the overflow fallback also
# failed), `_maybe_auto_compact` skips this many FURTHER steps before trying
# again, instead of repeating the whole (potentially slow, wait-heavy) attempt
# on every single step.
_COMPACTION_FAILURE_BACKOFF_STEPS = 5


def _content_text_for_serialize(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, dict):
                parts.append(f"[{block.get('type', 'content')} block]")
        return "".join(parts)
    return str(content) if content is not None else ""


def _serialize_transcript_for_summary(messages: list, *, max_chars: Optional[int] = None) -> str:
    """Flatten `messages` (Anthropic-shaped) to OpenCode's own plain-text
    head serialisation: `[User]: ...`, `[Assistant]: ...`, `[Assistant
    reasoning]: ...`, `[Assistant tool call]: name({...})`, `[Tool
    result]: <first 2000 chars>\\n[truncated]` -- one line group per
    message, in order. Used ONLY as the fallback body for the
    summarisation call itself (never logged, never sent as the ordinary
    per-step request) when the exact structured prefix overflows.

    H5c finding 22: `max_chars`, when given, bounds the TOTAL returned
    string (never just each individual tool_result) -- keeping only the
    HEAD (a message-group boundary is never split mid-group) and dropping
    any excess tail, with a trailing marker noting the cut. Without this,
    a transcript with many turns/short results could overflow the
    fallback call's OWN small context window a second time, with no
    further retry available."""
    lines: list = []
    truncated = False
    running_chars = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        group: list = []
        if role == "user":
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    group.append(f"[User]: {b['text']}")
                elif b.get("type") == "tool_result":
                    text = _content_text_for_serialize(b.get("content"))
                    head = text[:_SUMMARY_TOOL_RESULT_CHARS]
                    suffix = "\n[truncated]" if len(text) > _SUMMARY_TOOL_RESULT_CHARS else ""
                    group.append(f"[Tool result]: {head}{suffix}")
        elif role == "assistant":
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text" and b.get("text"):
                    group.append(f"[Assistant]: {b['text']}")
                elif btype == "thinking" and b.get("text"):
                    group.append(f"[Assistant reasoning]: {b['text']}")
                elif btype == "tool_use":
                    try:
                        args = json.dumps(b.get("input") or {}, ensure_ascii=False)
                    except (TypeError, ValueError):
                        args = str(b.get("input"))
                    group.append(f"[Assistant tool call]: {b.get('name')}({args})")
        if not group:
            continue
        group_chars = sum(len(g) + 1 for g in group)
        # H5c finding 22: cap the TOTAL size -- keep the HEAD, never split a
        # message's own group of lines in half. Always keep at least the
        # very first non-empty group even if it alone exceeds the budget
        # (an empty fallback body helps no one).
        if max_chars is not None and lines and running_chars + group_chars > max_chars:
            truncated = True
            break
        lines.extend(group)
        running_chars += group_chars
    text = "\n".join(lines)
    if truncated:
        text += _SUMMARY_FALLBACK_TRUNCATED_MARKER
    return text


_PROGRESS_CHUNK_CAP_CHARS = 200_000  # review finding 15: bound, never unbounded


class _BoundedChunks:
    """A `progress_cb`-shaped `.append(chunk)` collector that stops
    retaining new chunks once `_PROGRESS_CHUNK_CAP_CHARS` is reached (a
    trailing marker chunk explains the cut) -- the tool itself is
    unaffected (its OWN full output still reaches `tool_registry.
    dispatch`'s return value and gets capped/spilled normally by
    `_finalize_tool_result`; this only bounds the LIVE `tool_progress`
    event stream a long-running Bash/PowerShell command can produce)."""

    def __init__(self) -> None:
        self.chunks: list = []
        self._total = 0
        self._capped = False

    def append(self, chunk: str) -> None:
        if self._capped:
            return
        self._total += len(chunk)
        if self._total > _PROGRESS_CHUNK_CAP_CHARS:
            self.chunks.append("\n[... further live progress output omitted (still streaming to the final "
                                "result) ...]")
            self._capped = True
            return
        self.chunks.append(chunk)


class _EitherAbort:
    """Halo 2.0.1 (GLM-brief.md item 3): a `.is_set()`-only duck-typed union
    of two `threading.Event`s. `providers/stream.py`'s phase-1/phase-2
    abort-polling (grep-verified) never calls anything ELSE on the `abort`
    object it's given -- so this is a safe drop-in that lets `Session._step`
    force-close a silently-blocked upstream call via a dedicated per-attempt
    `steer_restart_event` WITHOUT ever touching the real `self.abort`,
    which stays reserved for a genuine user interrupt (Esc/Ctrl+C). Keeping
    them separate matters: if `self.abort` itself were (ab)used for this,
    `_step`'s own "was this a real interrupt or just a steer-restart" check
    afterward would be racy against a genuine interrupt arriving in the
    same window."""
    __slots__ = ("_a", "_b")

    def __init__(self, a: "threading.Event", b: "threading.Event") -> None:
        self._a = a
        self._b = b

    def is_set(self) -> bool:
        return self._a.is_set() or self._b.is_set()


def _steer_restart_when_silent_enabled() -> bool:
    """GLM-brief.md item 3 / W2-plan item 2: `steer.restart_when_silent`
    in `~/.halo/config.json`, default True. Read fresh on every `_step`
    call (cheap; this almost never changes mid-session) rather than cached
    on `Session`, so a test (or a user's `/config set`) flipping it takes
    effect on the very next model call."""
    from halo_harness.theme import get_config_value
    return bool(get_config_value("steer.restart_when_silent", default=True))


class _StepResult:
    def __init__(self, *, assistant_blocks, stop_reason, usage, reasoning, body, tool_call_flags=None,
                 finish_reason=None, latency_ms=None, ttft_ms=None, retries=0, status="ok",
                 responding_provider=None, ttfb_ms=None, first_reasoning_ms=None, first_text_ms=None,
                 first_tool_ms=None, reasoning_streamed=False):
        self.assistant_blocks = assistant_blocks
        self.stop_reason = stop_reason
        self.usage = usage
        self.reasoning = reasoning
        self.body = body  # the exact prebuilt_oai_body this step sent (finding 4's hash source)
        self.tool_call_flags = tool_call_flags or {}  # tool_use id -> {truncated_by_length|malformed_json,...}
        # H10 Part A: telemetry fields for `_account_usage`'s own
        # `append_usage(...)` call -- `finish_reason` mirrors `stop_reason`
        # (kept as its own field so a future dialect-specific value can
        # diverge without touching the Anthropic-shaped `stop_reason` every
        # other caller already depends on); `retries`/`status` describe
        # THIS successful call's own attempt ladder (a call that never
        # succeeds returns None from `_step`, not a `_StepResult` -- see
        # `_step`'s own `return None` sites for the terminal-failure path).
        self.finish_reason = finish_reason if finish_reason is not None else stop_reason
        self.latency_ms = latency_ms
        self.ttft_ms = ttft_ms
        self.retries = retries
        self.status = status
        # H10 Part A: OpenRouter's own per-chunk "provider" (which backing
        # inference host actually served this call) -- None for Databricks/
        # Anthropic-native routes, where `_account_usage` falls back to a
        # fixed label instead (see its own docstring).
        self.responding_provider = responding_provider
        # Halo 2.0.1 W2a telemetry (GLM-brief.md item 6 / HALO-2.0.1-
        # liveness-tips-brief.md Part A6): `ttfb_ms` is time-to-headers
        # (the first upstream event of ANY kind); `first_text_ms`/
        # `first_tool_ms`/`first_reasoning_ms` are time-to-first-token BY
        # KIND (None for a kind that never streamed this call);
        # `reasoning_streamed` is True iff reasoning arrived over MULTIPLE
        # separate wire chunks (vs. one lump, early or -- Databricks GLM,
        # per the brief's own open question -- at the very end) so rolo's
        # real session logs can answer which gateways actually stream it.
        self.ttfb_ms = ttfb_ms
        self.first_reasoning_ms = first_reasoning_ms
        self.first_text_ms = first_text_ms
        self.first_tool_ms = first_tool_ms
        self.reasoning_streamed = reasoning_streamed


def _assistant_block_is_replayable(b: dict) -> bool:
    """H5b finding 5: would this ONE block survive
    `providers.request.prepare_anthropic_messages`'s own replay filter (or
    is it simply worth keeping) -- used to decide whether a whole step's
    `assistant_blocks` are worth logging at all. A `tool_use` always
    survives (dispatch needs it logged so its `tool_result` has something
    to pair with). A `text` block survives only with real (non-whitespace)
    text -- a steer noticed at/before `message_start` never streamed any.
    A `thinking` block survives iff it has a real `signature` (finding 16:
    a SIGNED block with EMPTY text is the normal "display omitted" shape
    and must be kept; only an UNSIGNED block -- cut short before
    `signature_delta`, e.g. a steer mid-thinking -- is not replayable).
    Any other block type (an image, ...) is never filtered here."""
    btype = b.get("type") if isinstance(b, dict) else None
    if btype == "tool_use":
        return True
    if btype == "text":
        return bool((b.get("text") or "").strip())
    if btype == "thinking":
        return bool(b.get("signature"))
    return True



def generated_tokens_for_otpm(usage) -> "int | None":
    """Tokens the Databricks output-tokens-per-minute limit counts for one
    reply: output PLUS reasoning. `map_usage` reports reasoning separately
    from output since the 1.0.1 fix pass (so cost is never double-billed),
    but the OTPM budget is charged for every generated token, so the
    tracker must see their sum or it under-counts and the 429s it exists
    to prevent come back. None when the provider reported neither."""
    if not isinstance(usage, dict):
        return None
    out, reasoning = usage.get("output_tokens"), usage.get("reasoning_tokens")
    if out is None and reasoning is None:
        return None
    return int(out or 0) + int(reasoning or 0)

class Session:
    """One conversation against one model. `session_context.system_prompt`
    is computed ONCE by the caller and logged as the session's single
    `system` node -- every derived request reuses it byte-for-byte."""

    def __init__(
        self, *, cwd, model_ref: ModelRef, model_profile: ModelProfile,
        creds: Optional[ProviderCreds], state_dir, model_label: str, session_context,
        small_model_ref: Optional[ModelRef] = None, session_log: Optional[SessionLog] = None,
        max_turns: int = 50, openrouter_base_url: Optional[str] = None,
        extra_headers: Optional[dict] = None, effort: Optional[str] = None,
        effort_source: Optional[str] = None,
        permission_engine: Optional[PermissionEngine] = None,
        session_catalog: Optional[object] = None, mcp_manager: Optional[object] = None,
        hook_runner: Optional[HookRunner] = None,
        agents: Optional[dict] = None, routes: Optional[dict] = None, agent_depth: int = 0,
        agent_type_restriction: Optional[set] = None, abort: Optional[threading.Event] = None,
        agent_id: Optional[str] = None, job_registry: Optional[JobRegistry] = None,
        roles: Optional[dict] = None, cli_roles: Optional[dict] = None,
        cli_flags: Optional[dict] = None,
    ):
        # H9: identifies THIS session as a particular sub-agent (passed by
        # `agent/subagent.py`'s `_build_child_session`; the parent/main
        # session leaves this None). Two uses: (critical review finding 2)
        # namespaces `_permission_waiters` keys (see `_resolve_tool_call`'s
        # ask branch) so two PARALLEL children whose own tool_use ids
        # happen to collide (e.g. Kimi's per-call id counter starting
        # fresh at 0 in each child's own, separately-empty log) never
        # overwrite each other's live waiter slot in the dict they share
        # with the parent; and (finding 16) suppresses SessionStart(startup
        # /resume) below for a child -- it fires SubagentStart instead
        # (agent/subagent.py), never the user's own SessionStart hooks,
        # which used to re-run once per sub-agent spawned (3 sub-agents in
        # one session used to fire SessionStart(startup) 4 times).
        self.agent_id: Optional[str] = agent_id
        self.cwd = cwd
        self.model_ref = model_ref
        self.small_model_ref = small_model_ref
        self.model_profile = model_profile
        self.creds = creds
        self.state_dir = state_dir
        self.model_label = model_label
        self.effort = effort
        # Halo 2.0.1 W2a (HALO-2.0.1-liveness-tips-brief.md Part C): the
        # RAW value `--effort`/settings configured, BEFORE any clamping --
        # `self.effort` itself is the SENT value from here on (every branch
        # below, `_cmd_effort`, and `_switch_model` all overwrite it with
        # whatever `clamp_effort` returns), so this is the only place the
        # originally-requested string survives for `providers/effort.py::
        # requested_vs_sent` to compare against. None (unchanged) when
        # nothing was ever explicitly requested at all.
        self.effort_requested: Optional[str] = effort
        self.turn_count = 0
        self.max_turns = max_turns
        # H5 scope D: per-family fallback pricing (model.CostMeter._fallback_cost)
        # for whenever a response has no usage.cost of its own.
        self.cost_meter = CostMeter(price_in=model_profile.price_in, price_out=model_profile.price_out,
                                     price_cache_read=model_profile.price_cache_read,
                                     price_cache_write=model_profile.price_cache_write)
        self.tool_registry: ToolRegistry = session_context.tool_registry
        self.route = Route(provider=model_ref.provider, upstream_model=model_ref.model, dialect=model_ref.dialect)
        # item 22 remainder: state_dir threaded through so a Databricks
        # endpoint with a LEARNED reasoning_effort_with_tools rule (a prior
        # live 400 on this exact endpoint, no model_table.json row of its
        # own) gets it from session start, not only after re-learning it
        # live again this session.
        self.provider_profile: ProviderProfile = resolve_profile(self.route, state_dir=state_dir)
        # 1.0.1 hotfix 19: "high" is the Anthropic-family default when
        # NOTHING more specific was set anywhere upstream (`--effort`,
        # `/effort`, settings `effortLevel`/`modelSettings.<id>.effortLevel`
        # -- `headless.py::build_session` already resolved all of those into
        # `effort` before this constructor ever runs, so `effort is None`
        # here genuinely means "nothing configured"). Every OTHER family
        # keeps its existing "omit -> provider default" behaviour
        # (`self.effort` stays None) -- this is scoped to
        # `thinking_format == "anthropic_thinking"` alone, which is exactly
        # cc:/ant:/every Databricks Claude foundation/Bedrock-Claude route
        # (`resolve_profile`'s own anthropic-passthrough branch), never a
        # chat-dialect route. `_build_body_for_messages`'s `no_thinking`
        # call (the summariser) still explicitly forces `effort=None` for
        # THAT one call regardless of this default -- it reads `self.effort`
        # only in its OWN `else` branch, so it's unaffected.
        self.effort_source = effort_source
        # 1.0.1 fixpass finding 11: the "high" default is scoped to
        # ADAPTIVE-capable models only (Opus/Fable/Mythos always, Sonnet
        # 4.6+) -- a non-adaptive model (Haiku 4.5, Sonnet 4.5 or older)
        # has no `{type: "adaptive"}` mode at all, so "high" became a
        # budget_tokens value close to max_tokens (see map_effort_anthropic),
        # letting thinking crowd out the answer on every turn where 1.0.0
        # sent no thinking at all. A non-adaptive model keeps the old
        # "omit -> provider default" behaviour (`self.effort` stays None)
        # unless the user/settings explicitly set one (still clamped below).
        from halo_harness.providers.request import _anthropic_model_supports_adaptive_thinking
        if (self.effort is None and self.provider_profile.thinking_format == "anthropic_thinking"
                and _anthropic_model_supports_adaptive_thinking(self.model_ref.model)):
            self.effort = "high"
            self.effort_source = self.effort_source or "default"
        elif self.effort is None and self.provider_profile.default_effort_when_unset \
                and self.provider_profile.reasoning_default_effort:
            # Halo 2.0.1 (GLM-brief.md item 1): Databricks GLM's own default
            # for an omitted field is `max`, the most expensive level and the
            # "it pauses" report itself -- so "nothing configured" sends the
            # route default (`high`) explicitly instead of omitting the field.
            self.effort = self.provider_profile.reasoning_default_effort
            self.effort_source = self.effort_source or "default"
        elif self.effort is not None:
            from halo_harness.providers.profiles import clamp_effort
            clamped = clamp_effort(self.effort, self.provider_profile)
            if (self.provider_profile.default_effort_when_unset and self.effort_source == "settings"
                    and self.effort.lower() not in self.provider_profile.effort_values_supported
                    and self.provider_profile.reasoning_default_effort):
                # Same rule for an effort inherited from Claude Code's settings
                # (`effortLevel`, written for Claude models -- typically `xhigh`):
                # a value this route does not accept lands on the route default,
                # not on the clamp map's most expensive mapping. An explicit
                # `--effort`/`/effort` keeps the clamp map's answer (`xhigh -> max`).
                self.effort_requested = self.effort
                clamped = self.provider_profile.reasoning_default_effort
                self.effort_source = "default"
            if clamped != self.effort:
                self.effort = clamped
        self.openrouter_base_url = openrouter_base_url
        self.extra_headers = extra_headers or {}
        self._loop_breaker: dict = {}  # canonical (name,args) -> consecutive count, reset every turn
        # H9 OpenCode item 20: a period-2 ("ping-pong") doom-loop detector
        # layered ON TOP of the cumulative counter above -- catches a
        # strict A,B,A,B,... alternation between two DIFFERENT calls at the
        # SAME 3/5/8 remind/deny/end thresholds. Without this, a pure
        # ping-pong (e.g. Read fileX, Grep for Y, Read fileX, Grep for Y,
        # ...) only trips the plain per-key counter once EACH of A and B
        # individually reaches the threshold on its own -- roughly twice as
        # many total tool calls as a dedicated pair-counter needs.
        # `_loop_breaker_history` holds the last 2 canonical keys called
        # this turn, in order; `_loop_breaker_period2` counts, per ordered
        # pair (older_key, newer_key), how many times "..., older_key,
        # newer_key, older_key" has been observed. A plain immediate
        # repeat (A,A,A) never enters this path (history[-1] == key is
        # excluded below) -- that stays exclusively the counter above's job.
        self._loop_breaker_history: list = []
        self._loop_breaker_period2: dict = {}
        # H10 Part B4: `/improve`'s hint -- fires AT MOST once per session
        # (status-bar text + one `notification` event, never a card, never
        # a model call); `_maybe_yield_improve_hint` (called from
        # `_account_usage`) flips this the first time it fires.
        self._improve_hint_fired = False
        # must-do 5: an interrupt source (Bash kill/Esc -- none exists yet
        # in `-p`) sets this; `_step` passes it into `stream_completion`
        # (cuts phase 2 short) and observes it in every retry wait.
        # H6 scope B must-do ("sub-agents reuse steer/abort/... plumbing"):
        # a caller (subagent.py's `_build_child_session`) may share the
        # PARENT's own Event here instead -- a child then observes the
        # SAME abort signal through every existing `self.abort.is_set()`
        # check already threaded through `_step`/retry waits/streaming,
        # with no new logic of its own. Every other caller keeps getting a
        # fresh, private Event exactly as before (this param is additive).
        self.abort = abort if abort is not None else threading.Event()
        # H5c finding 7: only the Session that OWNS its abort Event (no
        # `abort=` was passed in -- every top-level Session) may `.clear()`
        # it at the start of a turn. A sub-agent shares the PARENT's own
        # Event object (see the comment above) so it observes the SAME
        # Esc/Ctrl+C signal -- but every child's `turn()` used to call
        # `self.abort.clear()` unconditionally too, on that SAME shared
        # object: verified with 5 concurrent Agent calls (a pool of 4 queues
        # one) -- Esc at +0.89s correctly interrupted children 0-3, but
        # child 4 started at +1.09s, ITS OWN `turn()` cleared the shared
        # Event, and both child 4 AND the parent's own next model call then
        # ran to completion as if Esc had never happened.
        self._owns_abort = abort is None
        # review finding 2: set by Controller.quit() (an atomic attribute
        # write, safe from any thread) BEFORE it queues run()'s `None`
        # sentinel -- see run()'s own comment for why this must be a
        # SEPARATE flag from `abort` (which a fresh turn clears).
        self._stopping = threading.Event()
        # scope 0(c): the steering side channel -- a plain list under a
        # lock, checked "per chunk and per tool" (review finding 6) from
        # INSIDE the running turn (_step's stream loop, _dispatch_tools'
        # per-call loop, _turn_body's post-dispatch/post-stream points),
        # never popped from any other thread.
        self._busy = threading.Event()
        self._steer_lock = threading.Lock()
        self._steer_queue: list = []
        # finding 5: steer text still queued when `turn()`'s `finally` runs
        # (never reached a safe point to apply) -- drained there, read and
        # cleared by `run()` right after, worker-thread-only on both ends
        # (no lock needed for THIS attribute: the write happens-before the
        # read, same thread, `_pump_turn` -> `run()` is fully sequential).
        self._leftover_steer_texts: list = []
        # H5c finding 14: `@file` mentions ingested from a steer, `@path`
        # snapshots from a prompt-kind slash command, and an inline `!cmd`
        # pair are all UI-thread (or a Textual worker thread, for `!cmd`)
        # writes to `self.log` that must never race the SESSION's own
        # worker thread mid-turn -- queued here (same `_steer_lock`) while
        # busy, applied by `_apply_pending_steers_events` at the SAME safe
        # point a steer's own text is (see `queue_log_write`).
        self._pending_worker_writes: list = []
        # H2 scope D: ONE PermissionEngine + ONE pair of read_cache/
        # bash_state dicts for the session's WHOLE lifetime -- every
        # ToolContext built in `_dispatch_tools` shares these same dict
        # instances (never a fresh one per call), which is what makes
        # Write's must-Read-first check and Bash's `cd` persistence work
        # across separate tool_use calls. `permission_engine=None` (a bare
        # Session in a unit test) means "allow everything" (auto, no
        # rules) so existing tests that never set one up keep working.
        self.permission_engine = permission_engine or PermissionEngine(mode="auto", cwd=cwd)
        self._read_cache: dict = {}
        self._bash_state: dict = {"cwd": cwd}
        # W4a CwdChanged: compared against `self._bash_state["cwd"]` after
        # every finalized Bash call (`_fire_cwd_changed`, called from
        # `_finalize_tool_result` alongside FileChanged) -- `tools/bash.py`
        # owns the actual `cd`-persistence logic and has no hook_runner of
        # its own to fire from, so the SESSION notices the change instead,
        # right after the call that caused it finishes.
        self._last_known_bash_cwd = cwd
        # finding 10 / H3 must-do: Bash/PowerShell/MCP CHILD PROCESSES get
        # `settings.effective_env` (shell < user < trusted project/local <
        # flag < policy -- so a settings.json `env` block, e.g. a PATH or
        # proxy override, actually reaches them) minus every secret the
        # harness itself loaded from its own env file plus the fixed
        # provider-token key list -- never the harness's raw `os.environ`,
        # which would otherwise leak OPENROUTER_API_KEY/DATABRICKS_TOKEN/...
        # into a routine `env`/`printenv` tool call's transcript. A bare
        # `session_context` without `.settings` (some unit tests) falls
        # back to the process's own environment, stripped the same way.
        settings = getattr(session_context, "settings", None)
        raw_env = settings.effective_env if settings is not None else dict(os.environ)
        self.tool_env: dict = tool_child_env(raw_env)
        # W4a: `cli_flags` -- the plain dict `cli_flags.cli_flags_from_args`
        # builds from argv, shared verbatim by -p and the TUI (headless.
        # build_session/tui.bootstrap.build_controller both thread it
        # through unchanged) so a flag's meaning can never drift between the
        # two launch paths. Every consumer below defaults safely when the
        # key is absent -- an old caller that never passes `cli_flags` at
        # all (every pre-W4a test) behaves byte-for-byte as before.
        self.cli_flags: dict = dict(cli_flags or {})
        if self.cli_flags.get("environment"):
            # `--environment KEY=VALUE` (repurposed for a standalone harness
            # -- see cli.py's own flag-table comment): merged in last, so it
            # wins over whatever the settings-env chain already resolved.
            self.tool_env = {**self.tool_env, **self.cli_flags["environment"]}
        self.fallback_models: list = list(self.cli_flags.get("fallback_models") or [])
        self.forward_subagent_text: bool = bool(self.cli_flags.get("forward_subagent_text"))
        self.include_hook_events: bool = bool(self.cli_flags.get("include_hook_events"))
        self.permission_prompt_tool: Optional[str] = self.cli_flags.get("permission_prompt_tool")
        self.permission_prompts: Optional[str] = self.cli_flags.get("permission_prompts")
        self._system_prompt_snapshot_mode: str = self.cli_flags.get("system_prompt_snapshot") or "off"
        self._snapshotted_system_prompt: Optional[str] = None
        # W4a `--include-hook-events`: every `_run_hook`/`_run_hook_stop`
        # call appends one entry here when enabled -- drained by `output.
        # StreamJsonSink` (stream-json's own `hook_event` lines) after each
        # turn event, never polled any other way.
        self._hook_event_log: list = []
        # H5 scope B: kept for re-injection after compaction
        # (claude_md_text()/memory_snapshot_text()) -- session_context was
        # a constructor-only local before this milestone.
        self.session_context = session_context
        # W4a ConfigChange: one mtime per settings LAYER that has a real
        # file (`SettingsLayer.path` -- None for managed/policy-default
        # layers with no file on disk), snapshotted now (AFTER `self.
        # session_context` is assigned just above -- `_snapshot_config_
        # mtimes` reads it); `_check_config_change` (called once per turn,
        # the natural "check for drift" cadence a long-lived session has)
        # compares against this and fires per CHANGED path, never a
        # background filesystem watcher.
        self._config_mtimes: dict = self._snapshot_config_mtimes()
        # D-CFG: "the harness reads ... CLAUDE_CODE_AUTO_COMPACT_WINDOW ...
        # from [effective_env]" -- same `raw_env` precedence chain (shell <
        # user < trusted project/local < flag < policy) everything else on
        # this line already uses, not a bare os.environ re-read.
        self._compaction_knobs = resolve_knobs(settings, raw_env)
        # Updated by `_account_usage` from each reply's real `input_tokens`;
        # None until the first reply lands, in which case the auto-compact
        # check falls back to a rough estimate of the about-to-be-sent
        # request instead (see `_maybe_auto_compact`).
        self._last_prompt_tokens: Optional[int] = None
        # finding 1: set True whenever `_maybe_auto_compact` just ran a
        # compaction, cleared at the top of its NEXT call -- guards against
        # back-to-back auto-compaction (two compactions with no real model
        # step in between) when a single compaction didn't free enough room
        # (e.g. one oversized verbatim-tail unit that alone exceeds the
        # trigger), which would otherwise loop forever re-summarising the
        # same freshly-compacted history.
        self._just_compacted: bool = False
        # H5c finding 3/22: how many MORE steps to skip auto-compaction for
        # after a FAILED attempt (never retried immediately -- see
        # `_maybe_auto_compact`'s own docstring).
        self._compaction_backoff_remaining: int = 0
        # print-mode `ask` denials accumulate here for the json result's
        # `permission_denials` (halo_harness/output.py reads this list).
        self.permission_denials: list = []
        # U2/D-Contract: `interactive=True` (set by the TUI's Controller, and
        # ONLY by it) makes an `ask` decision REALLY block -- the turn emits
        # `permission_request` and then waits on a threading.Event keyed by
        # the tool_use id until the UI calls `resolve_permission`. H2b's
        # non-blocking "unavailable in this session" denial stays the
        # behaviour for `-p` and for a bare Session in a unit test.
        self.interactive = False
        # H5c finding 8: a FOREGROUND sub-agent of an interactive parent
        # gets a live, answerable permission card too, WITHOUT flipping
        # `self.interactive` itself (which also gates AskUserQuestion/
        # ExitPlanMode below -- out of this finding's scope, and would
        # hang a child forever on those since only `_permission_waiters`,
        # not `_question_waiters`/`_plan_waiters`, is ever shared with the
        # parent). Set by `agent/subagent.py`'s `_build_child_session`;
        # every other Session (including a print-mode/background child)
        # leaves this False and keeps the old immediate-denial fallback.
        self._subagent_live_asks = False
        self._permission_waiters: dict = {}
        # U2: the AskUserQuestion round trip parks here, keyed by tool_use
        # id, exactly like `_permission_waiters`.
        self._question_waiters: dict = {}
        # W4a: the Controller's own `events.put` (set by `run()`, the
        # worker-thread command pump -- None for a `-p`/test Session that
        # never calls `run()` at all) -- `agent/subagent.py`'s `_bg_run`
        # uses this to forward a BACKGROUND child's live permission/
        # question/plan asks into the SAME dock a foreground child's
        # already do, since a background child's own `turn()` is never
        # drained by anything else that could relay them.
        self._event_sink = None
        # U2: the Controller installs one before `run()`; a bare Session
        # (a unit test, print mode) reports 0/0 in its status events.
        self.mcp_status_fn = None
        # H3 scope C: the frozen-catalog + lazy-load owner (agent/catalog.
        # SessionCatalog) and the McpManager it draws deferred tools from
        # -- both None for a bare Session (every pre-H3 test, and any
        # session with zero MCP servers). `on_grow` is wired to append a
        # new `meta` log node so the growing wire catalog is itself logged
        # (model-visible means logged); `_dispatch_tools` threads
        # `self.session_catalog` into every ToolContext as `catalog=`.
        self.session_catalog = session_catalog
        self.mcp_manager = mcp_manager
        if self.session_catalog is not None:
            self.session_catalog.on_grow = self._on_catalog_grow
        # H4 scope A/B: the hook runner for this session's whole lifetime
        # (its Stop-cap counter and `once`-dedup state must persist across
        # calls) -- None for a bare Session (every pre-H4 test, `--bare`,
        # or a settings tree with no hooks configured at all: every call
        # site below guards on `self.hook_runner is not None`).
        self.hook_runner = hook_runner
        # W3b item 11: THIS session's own per-turn timeline instance --
        # never the `debug_timeline` module's shared default -- so two
        # parallel sub-agent Sessions (agent/subagent.py's own
        # ThreadPoolExecutor batch dispatch) each keep their own turn
        # history instead of racing over shared module-level state. See
        # `debug_timeline.TurnTimeline`'s own docstring for the full
        # rationale; `turn()`'s wrapper and `_run_hook`/the permission-wait
        # call site below are its only writers.
        from halo_harness.debug_timeline import TurnTimeline
        self._timeline = TurnTimeline()
        # D-CFG: `stop_hook_active` is True only on a RE-ENTRANT Stop-hook
        # evaluation within the SAME "the model tried to stop" attempt --
        # reset at the start of every new turn() call, never carried from
        # a previous turn.
        self._stop_hook_active = False
        # H6 scope B: the Agent/Task tool's own context -- `agent_depth`
        # is 0 for every top-level session and 1 for a sub-agent's own
        # child Session (agent/subagent.py._build_child_session passes
        # `agent_depth=parent's + 1`); `run_agent_call` refuses outright
        # once depth reaches MAX_DEPTH, which is what actually enforces
        # "a sub-agent cannot itself spawn further sub-agents" -- `agents`
        # (the discovered AgentSpec catalog) and `routes` (routes.json,
        # for model-ref resolution) are supplied by whoever builds this
        # Session (headless.py/tui/bootstrap.py); both default to {} for
        # every pre-H6 test and a bare Session, in which case the Agent
        # tool still works but only ever knows the 3 built-in specs.
        # V2c (H15): `roles`/`cli_roles` (the persisted role table and this
        # run's own `--role name=model` CLI overrides -- both resolved ONCE
        # by whoever builds this Session, headless.py/tui/bootstrap.py) are
        # threaded through exactly like `agents`/`routes` above; both
        # default to {} for every pre-V2c test and a bare Session, in which
        # case a role-bearing agent just falls through to the parent's own
        # model (its documented "orchestrator" default anyway).
        self.roles = roles or {}
        self.cli_roles = cli_roles or {}
        self.agent_runtime = AgentRuntime(parent=self, agents=(agents or {}), routes=(routes or {}),
                                           role_table=self.roles, cli_role_overrides=self.cli_roles,
                                           depth=agent_depth)
        self.agent_type_restriction = agent_type_restriction
        # H6 scope F: background sub-agent completions wait here (a plain
        # list under a lock, exactly like the steering queue) until the
        # NEXT turn() call applies them as a user-role notice (dsh: "report
        # completion as a user-role notice in the next step").
        self._pending_agent_notices: list = []
        self._agent_notices_lock = threading.Lock()
        # H8 scope A: background Bash jobs (`Bash(run_in_background:true)`
        # and a foreground command moved to the background after its own
        # timeout) -- one JobRegistry per session, threaded through every
        # ToolContext as `job_registry=` (Bash/BashOutput/TaskStop all read
        # it). `_pending_job_notices`/`_job_notices_lock` mirror
        # `_pending_agent_notices`/`_agent_notices_lock` exactly (same dsh
        # rule, kept in a SEPARATE list so a bash job's completion is never
        # confused with a sub-agent's in the log) -- see
        # `_apply_pending_job_notices`.
        #
        # H9 whole-tree review finding 3: `job_registry=` lets a caller
        # (agent/subagent.py's `_build_child_session`) hand a CHILD session
        # the SAME registry instance the parent already owns, instead of
        # every sub-agent silently building its own private one. Before
        # this, a background Bash job started inside a sub-agent was
        # invisible to the parent's own `kill_all()` (Controller.quit(),
        # `-p`'s own `finally`) -- verified on WSL: a sub-agent's own
        # `sleep 301 &` outlived a normal `-p` exit entirely -- AND its
        # completion notice was pushed onto the CHILD Session's own
        # `_pending_job_notices`, which nothing ever drains once
        # `run_agent_call` returns and the child object is discarded (a
        # foreground child runs exactly one turn; a background child's
        # thread exits once `_bg_run` returns). Sharing the registry fixes
        # both at once: `kill_all()` on either session's `self.job_registry`
        # now reaches every job either one ever started (MAX_DEPTH=1 means
        # there is only ever one level of child, so no further propagation
        # is needed), and `JobRegistry.parent` (fixed at construction, to
        # whichever Session builds a FRESH one -- always the true top-level
        # session, since every child now passes one in) means a job a child
        # starts reports its completion notice to the PARENT's own
        # `_pending_job_notices`, where the parent's NEXT turn actually
        # applies it -- the only session that outlives the child's own turn.
        self.job_registry = job_registry if job_registry is not None else JobRegistry(parent=self)
        self._pending_job_notices: list = []
        self._job_notices_lock = threading.Lock()
        # H5b finding 2: tool_result pruning's "outside the 40k-token
        # protection window -> stub it" decision is committed HERE, in
        # batches of >= PRUNE_REBALANCE_CHUNK_TOKENS, rather than being
        # recomputed (and therefore drifting by roughly one message) on
        # every single request -- see `_pruned_messages_for_wire` and
        # agent/prune.py's own module docstring. Reset on `/clear` and
        # after a successful compaction (both already discard/rewrite the
        # history these ids refer to).
        self._prune_committed_stub_ids: set = set()
        self._prune_pending_stub_tokens: dict = {}
        # H6 scope C: plan mode's pending ExitPlanMode wait -- mirrors
        # `_permission_waiters`/`_question_waiters` (a request_id-keyed
        # dict + threading.Event) but only ever has ONE live entry at a
        # time (a whole turn blocks on ExitPlanMode; PlanCard's own reply
        # carries no request_id to key on either -- see resolve_plan).
        self._plan_waiters: dict = {}
        self._pending_plan_id: Optional[str] = None

        self.log = session_log or SessionLog(cwd)
        # H9 whole-tree review finding 21: this session's own tool-results
        # spill directory is only knowable once `self.log` exists (the
        # permission engine is normally built and handed in BEFORE that) --
        # see PermissionEngine.tool_results_dir's own docstring. Every
        # Session gets one, including a bare `Session()` in a unit test
        # that never set up a real PermissionEngine (the default one built
        # two lines above this in `__init__` still benefits).
        if self.permission_engine is not None:
            self.permission_engine.tool_results_dir = self.log.dir / self.log.session_id / "tool-results"
        existing_nodes = self.log.nodes()
        if not existing_nodes:
            # finding 4: when a SessionCatalog exists, `.names` (not
            # `.definitions()`) is the source of truth for wire-catalog
            # ORDER from this point on -- at this exact moment the two
            # agree (the catalog was just frozen name-sorted), but only
            # `.names` keeps agreeing after a later lazy-load append.
            initial_tools = (self.tool_registry.definitions_for(self.session_catalog.names)
                              if self.session_catalog is not None else self.tool_registry.definitions())
            self.log.append_meta(
                model=model_ref.raw, cwd=str(cwd),
                system_prompt_bytes=len(session_context.system_prompt.encode("utf-8")),
                tools=initial_tools,
            )
            self.log.append_system(session_context.system_prompt)

            claude_md = session_context.claude_md_text()
            if claude_md:
                self.log.append_snapshot([{"type": "text", "text": claude_md}], kind="claude_md")
                self._fire_instructions_loaded(claude_md)
            memory_text = session_context.memory_snapshot_text()
            if memory_text:
                self.log.append_snapshot([{"type": "text", "text": memory_text}], kind="memory_index")
            env_text = session_context.environment_snapshot_text(model_label)
            self.log.append_snapshot([{"type": "text", "text": env_text}], kind="environment")
            # finding 12: the deferred tool NAMES (ToolSearch/deferred-load
            # call site), listed up front exactly once -- Claude Code's own
            # reminder to the model, matching this codebase's OWN
            # ToolSearch tool description's promise that a deferred name
            # "may already appear in this tool's own listings without a
            # schema". A snapshot is a user-role block (agent/log.py), so
            # this is the "cache-stable user-role snapshot" the finding
            # asks for: appended ONCE, here, only for a brand-new session
            # (never on resume -- the `else:` branch below never re-runs
            # this), so it never shifts the cache prefix turn to turn even
            # as `session_catalog.deferred` itself later shrinks/grows.
            if self.session_catalog is not None and self.session_catalog.deferred:
                deferred_names = sorted(self.session_catalog.deferred)
                reminder = (
                    "The following deferred tools are now available via ToolSearch. Their schemas "
                    "are NOT loaded -- calling them directly will fail with InputValidationError. Use "
                    "ToolSearch with query \"select:<name>[,<name>...]\" to load tool schemas before "
                    "calling them:\n" + "\n".join(deferred_names)
                )
                self.log.append_snapshot([{"type": "text", "text": reminder}], kind="deferred_tools")
            self._fire_session_start("startup")
            self._fire_directory_added_at_start()
            self._fire_setup_if_first_run()
            self._fire_worktree_created()
        else:
            # finding 4: RESUMING an existing log -- NEVER re-append
            # meta/system/snapshots (a second system node makes
            # derive_request raise LogAssemblyError on every later call).
            # Verify byte-stability instead of blindly trusting it (a code/
            # config change between runs is a real possibility, not a bug
            # to crash over), and re-pair any tool_use an earlier process
            # left dangling (killed before its own synthesize_missing_
            # results ran) before this session's first new user node.
            logged_system = next((n.get("text") for n in existing_nodes if n.get("type") == "system"), None)
            if logged_system is not None and logged_system != session_context.system_prompt:
                log.warning(
                    "resumed session's logged system prompt differs from this process's rebuilt one "
                    "(tool registry or config changed since the original run) -- keeping the LOGGED text"
                )
            synthesize_missing_results(self.log, reason="ABORTED_BEFORE_DISPATCH")
            # H9 whole-tree review finding 28: rebuild the prune-commit set
            # from the log's own `prune_commit` marker nodes (see
            # `append_prune_commit`'s docstring and `_pruned_messages_for_
            # wire`'s own commit site) -- without this, a resumed process's
            # `self._prune_committed_stub_ids` (set to `set()` a few lines
            # up in this SAME `__init__`, before `self.log` even existed)
            # stayed empty forever, silently sending a LARGER wire prefix
            # (every previously-stubbed tool_result back at full size) than
            # what the model actually saw pre-restart. Unions EVERY
            # `prune_commit` node ever logged, including ones from before a
            # LATER `/clear`/compaction (which resets the live set to empty
            # via `_reset_prune_state` -- itself never logged, deliberately:
            # see its own docstring) -- reconstructing a few now-irrelevant
            # ids this way is harmless, not a correctness bug: `prune_
            # messages` only stubs an id it finds an ACTUAL tool_result
            # block for, so an id that no longer appears post-compaction is
            # silently a no-op, the exact same tolerance this file's own
            # pending-batch pruning already relies on a few lines above.
            for _node in existing_nodes:
                if _node.get("type") == "prune_commit":
                    ids = _node.get("stub_ids")
                    if isinstance(ids, list):
                        self._prune_committed_stub_ids |= {i for i in ids if isinstance(i, str)}
            self._fire_session_start("resume")
            self._fire_directory_added_at_start()

    def _run_hook(self, event: str, payload: dict, **kwargs):
        """W3b item 11: every `self.hook_runner.run(...)` call site in this
        class (and `agent/subagent.py`'s own `child._run_hook(...)`, a
        sub-agent's child Session) goes through here instead -- the ONE
        choke point every hook invocation already shares, regardless of
        which event (SessionStart, PreToolUse, PermissionRequest,
        PostToolBatch, ...) fired it -- so the per-turn timeline
        (`self._timeline.record_hook`) sees each hook's own event name and
        real wall-clock duration. A pure timing wrapper: the real call's
        own `HookOutcome` return value is passed through unchanged, and a
        hook that fires outside any turn (SessionStart/SessionEnd) still
        times correctly -- `TurnTimeline.record_hook` itself is just a
        no-op when there's no active turn to attach the entry to."""
        t0 = time.monotonic()
        outcome = self.hook_runner.run(event, payload, **kwargs)
        self._timeline.record_hook(event, (time.monotonic() - t0) * 1000.0)
        self._record_hook_event(event, outcome)
        return outcome

    def apply_next_fallback_model(self) -> bool:
        """W4a `--fallback-model`: pops the next untried model off
        `self._fallback_remaining` (seeded from `self.fallback_models` at
        the start of each turn -- see `_turn_inner`'s own reset of it,
        matching claude's own "re-tries the primary at the start of each
        user turn") and switches this session onto it via the SAME
        `set_model` path `/model` itself uses, so the route/profile/
        provider_profile stay consistent with a live model switch rather
        than a half-updated `self.model_ref`. Returns False (a no-op) when
        the list is empty -- the caller's own exhausted-retries error path
        is unaffected either way."""
        if not getattr(self, "_fallback_remaining", None):
            return False
        from halo_harness.model import parse_model_ref, resolve_model_profile
        next_raw = self._fallback_remaining.pop(0)
        routes = self.agent_runtime.routes if self.agent_runtime is not None else {}
        try:
            ref = parse_model_ref(next_raw, routes)
            profile = resolve_model_profile(ref, self.state_dir, routes)
        except Exception:
            return self.apply_next_fallback_model()  # an unresolvable entry is skipped, not fatal
        self.set_model(ref, profile, self.creds)
        return True

    def _run_hook_stop(self, event: str, **kwargs):
        """W3b item 11: the `run_stop(...)` counterpart to `_run_hook`
        above (Stop/SubagentStop's own consecutive-block-cap variant of
        `run`, `hooks.py::HookRunner.run_stop`) -- same timing-wrapper
        purpose, recorded under the same event name (`agent/subagent.py`'s
        own `child._run_hook_stop(...)` reaches this for a sub-agent's
        SubagentStop too, `child` being that sub-agent's own Session)."""
        t0 = time.monotonic()
        outcome = self.hook_runner.run_stop(event, **kwargs)
        self._timeline.record_hook(event, (time.monotonic() - t0) * 1000.0)
        self._record_hook_event(event, outcome)
        return outcome

    def _record_hook_event(self, event: str, outcome) -> None:
        """W4a `--include-hook-events`: a tiny, JSON-safe record of this ONE
        hook invocation's outcome -- stream-json's own `hook_event` line
        shape (`output.py::StreamJsonSink`). A no-op (not even a list
        append) when the flag is off, so this costs nothing for every other
        caller/test."""
        if not getattr(self, "include_hook_events", False):
            return
        self._hook_event_log.append({
            "hook_event_name": event, "blocked": bool(outcome.blocked),
            "permission_decision": outcome.permission_decision,
            "continue": outcome.continue_,
        })

    def drain_hook_events(self) -> list:
        """Pops and returns every hook-event record queued since the last
        call -- `output.StreamJsonSink` drains this after each turn event it
        emits, so hook lines interleave with the turn's own output in the
        order they actually happened."""
        events_out, self._hook_event_log = self._hook_event_log, []
        return events_out

    def _fire_session_start(self, source: str) -> None:
        """H4 scope B: SessionStart(startup|resume|clear|compact) -- `resume`
        fires from `__init__`, `compact` from `_run_compaction`, `clear`
        from `/clear` (U5). Hook-added `additionalContext` becomes a
        user-role SNAPSHOT in the log (never the system node -- brief B).

        H9 review finding 16: a sub-agent (`self.agent_id is not None`)
        NEVER fires the user's own SessionStart hooks at all -- it fires
        SubagentStart instead (agent/subagent.py's own start event). Before
        this guard, every child's brand-new (always-empty) log made its
        `__init__` take the "startup" branch above, so spawning N sub-
        agents in one session re-ran the user's SessionStart(startup) hook
        N extra times (a session with 3 sub-agents ran it 4 times total)."""
        if self.agent_id is not None:
            return
        if self.hook_runner is None or not self.hook_runner.has_hooks("SessionStart"):
            return
        payload = self.hook_runner.payload("SessionStart", extra={"source": source})
        # H5c finding 12: `abort` threaded through -- Esc during a slow
        # SessionStart command hook (startup/resume/clear/compact) used to
        # be completely ignored.
        outcome = self._run_hook("SessionStart", payload, matched=source, abort=self.abort)
        if outcome.additional_context:
            self.log.append_snapshot([{"type": "text", "text": outcome.additional_context}], kind="hook_context")
        # finding 6: re-read on EVERY SessionStart source, not just
        # startup/resume -- a SessionStart(compact)/(clear) hook that
        # writes/rewrites CLAUDE_ENV_FILE must take effect immediately too.
        # This snapshot-merge is the fallback path for tools that don't
        # get a live per-call `source` (PowerShell, MCP stdio spawns,
        # other hook subprocesses); the Bash tool's OWN per-call sourcing
        # (tools/bash.py, via `ctx.env_file`) never depends on this at all.
        from halo_harness.hooks import env_file_path, read_env_file_exports
        # H9 whole-tree review finding 5: `base_env=self.tool_env` -- the
        # ALREADY tool_child_env()-stripped env (no provider secret keys) --
        # not `read_env_file_exports`'s own default (the harness's raw,
        # UNSTRIPPED `os.environ`, which DOES still carry
        # OPENROUTER_API_KEY/DATABRICKS_TOKEN/... since that's what this
        # process was started with). That default fed TWO things: (a) the
        # actual subprocess env the `. "$file"` sourcing shell runs with,
        # and (b) the "what changed" diff baseline. A hook itself always
        # sees the stripped env (HookRunner already uses tool_child_env),
        # but a LINE that hook writes into $CLAUDE_ENV_FILE is not run by
        # the hook -- it's run by THIS sourcing call, later -- so a hook
        # that appends e.g. `echo "$OPENROUTER_API_KEY" > f` (a real key
        # reference, invisible to the hook process itself) had that `$VAR`
        # expand against the raw env right here, writing the ACTUAL secret
        # to disk. Verified on WSL.
        exports = read_env_file_exports(env_file_path(self.log.session_id), base_env=self.tool_env)
        if exports:
            self.tool_env = {**self.tool_env, **exports}

    def _snapshot_config_mtimes(self) -> dict:
        out: dict = {}
        settings = getattr(self.session_context, "settings", None)
        for layer in (getattr(settings, "layers", None) or []):
            path = getattr(layer, "path", None)
            if path is None:
                continue
            try:
                out[str(path)] = Path(path).stat().st_mtime
            except OSError:
                pass
        return out

    def _check_config_change(self) -> None:
        """W4a: ConfigChange -- "settings file changed on disk" (removed
        from NOT_EMITTED_V1). Checked once per turn (see `_config_mtimes`'s
        own comment) -- a session that never starts another turn never
        checks again, same honest "no background watcher" scope every other
        best-effort mechanism in this file already has."""
        if self.hook_runner is None or not self.hook_runner.has_hooks("ConfigChange"):
            return
        current = self._snapshot_config_mtimes()
        for path, mtime in current.items():
            if self._config_mtimes.get(path) != mtime:
                payload = self.hook_runner.payload("ConfigChange", extra={"path": path})
                try:
                    self._run_hook("ConfigChange", payload, matched=path)
                except Exception:
                    pass
        self._config_mtimes = current

    def fire_user_prompt_expansion(self, original_text: str, expanded_text: str) -> Optional[str]:
        """W4a: UserPromptExpansion -- "after @-mention and `!cmd` expansion"
        (removed from NOT_EMITTED_V1). Called by the TWO callers that
        actually DO this expansion (`controller.py`'s prompt-kind slash-
        command path, `headless.py`'s equivalent) right after `commands.
        registry.expand_command_body` substitutes `$ARGUMENTS`/`@path`/
        `` !`cmd` `` -- a plain typed prompt's own `@mention`s are never
        INLINED into the text itself (they become separate snapshot blocks,
        several docstrings in this codebase are explicit about that), so
        there is nothing textual to call "expansion" on for that path; this
        is scoped to the one case where the text really does change.
        Returns hook-added `additionalContext` (one of this event's own
        `_CONTEXT_ONLY_EVENTS`) so the caller can fold it in as a snapshot,
        or None when nothing fired/nothing to add."""
        if self.hook_runner is None or not self.hook_runner.has_hooks("UserPromptExpansion"):
            return None
        if expanded_text == original_text:
            return None
        payload = self.hook_runner.payload("UserPromptExpansion",
                                            extra={"original_prompt": original_text, "prompt": expanded_text})
        try:
            outcome = self._run_hook("UserPromptExpansion", payload, matched="")
        except Exception:
            return None
        return outcome.additional_context or None

    def _fire_message_display(self, blocks: list) -> None:
        """W4a: MessageDisplay -- "each assistant message shown" (removed
        from NOT_EMITTED_V1). Fires at the main streaming completion's own
        log-append point (the overwhelming common case); an interrupted/
        plan/compaction-replay/cc: bridge message does not ALSO fire this
        -- a documented scope cut, not an oversight (see the worker report).
        Text-only (a tool_use-only reply with no text block is skipped --
        there is nothing to "display")."""
        if self.hook_runner is None or not self.hook_runner.has_hooks("MessageDisplay"):
            return
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        if not text:
            return
        payload = self.hook_runner.payload("MessageDisplay", extra={"message": text})
        try:
            self._run_hook("MessageDisplay", payload, matched="")
        except Exception:
            pass

    def _fire_instructions_loaded(self, claude_md_text: str) -> None:
        """W4a: InstructionsLoaded -- "CLAUDE.md chain loaded" (removed from
        NOT_EMITTED_V1). Fires every time the chain is (re)loaded into the
        log as a snapshot -- startup, resume, and after a compaction -- same
        set of call sites `kind="claude_md"` itself already has."""
        if self.hook_runner is None or not self.hook_runner.has_hooks("InstructionsLoaded"):
            return
        payload = self.hook_runner.payload("InstructionsLoaded", extra={"char_count": len(claude_md_text)})
        try:
            self._run_hook("InstructionsLoaded", payload, matched="")
        except Exception:
            pass

    def _fire_stop_failure(self, turn_no: int, reason_text: str) -> None:
        """W4a: StopFailure -- "a turn that ends in error" (removed from
        NOT_EMITTED_V1). Fired ALONGSIDE (never instead of) the ordinary
        `turn_done(reason="error")` the caller still yields right after --
        observational, like PermissionDenied; nothing it returns changes an
        already-decided outcome."""
        if self.hook_runner is None or not self.hook_runner.has_hooks("StopFailure"):
            return
        payload = self.hook_runner.payload("StopFailure", prompt_id=f"turn_{turn_no}", extra={"reason": reason_text})
        try:
            self._run_hook("StopFailure", payload, matched="error")
        except Exception:
            pass

    def _fire_setup_if_first_run(self) -> None:
        """W4a: Setup -- "first run / init" (removed from NOT_EMITTED_V1).
        Fires exactly ONCE ever on this machine (a `<state>/.setup_done`
        marker, created right after, same directory every other first-run
        state already lives under), on the first brand-new session's own
        "startup" path -- never on resume, never again on a later launch.
        `halo init`'s own wizard is a SEPARATE, optional setup flow a user
        may never run at all; this is the one trigger guaranteed to exist
        for every box."""
        if self.hook_runner is None or not self.hook_runner.has_hooks("Setup"):
            return
        marker = Path(self.state_dir) / ".setup_done"
        if marker.exists():
            return
        payload = self.hook_runner.payload("Setup", extra={})
        try:
            self._run_hook("Setup", payload, matched="")
        except Exception:
            pass
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("", encoding="utf-8")
        except OSError:
            pass

    def _fire_worktree_created(self) -> None:
        """W4a: WorktreeCreated -- "with `--worktree`" (removed from
        NOT_EMITTED_V1). `headless.run_print_mode` creates the worktree
        itself, before this Session (and its hook_runner) exists -- the
        path rides through `cli_flags["_worktree_created_path"]` so this
        fires from the session that actually lives in it. WorktreeRemoved's
        own trigger (an explicit `claude rm`-equivalent cleanup command)
        is not built in this round -- see the worker report; `halo_harness.
        worktree.remove_worktree` exists and is tested standalone, ready
        for whatever command ends up calling it."""
        path = self.cli_flags.get("_worktree_created_path")
        if not path or self.hook_runner is None or not self.hook_runner.has_hooks("WorktreeCreated"):
            return
        payload = self.hook_runner.payload("WorktreeCreated", extra={"path": path})
        try:
            self._run_hook("WorktreeCreated", payload, matched=path)
        except Exception:
            pass

    def _fire_directory_added_at_start(self) -> None:
        """W4a: DirectoryAdded -- "`--add-dir`, `/add-dir`" (removed from
        NOT_EMITTED_V1). `/add-dir` itself is a pre-existing stub in this
        build (`commands/builtins.py::_cmd_add_dir`: "needs a running
        session to extend" -- a SEPARATE, not-yet-built capability, out of
        this hook's own scope to add), so the only real trigger today is
        every directory this session actually started with (`--add-dir` /
        settings `permissions.additionalDirectories`, already merged into
        `self.permission_engine.extra_dirs` by the time this runs) -- fired
        once per directory, at both startup and resume (an unchanged set on
        resume is a harmless re-announcement, never tracked as a diff)."""
        if self.hook_runner is None or not self.hook_runner.has_hooks("DirectoryAdded"):
            return
        for d in self.permission_engine.extra_dirs:
            payload = self.hook_runner.payload("DirectoryAdded", extra={"path": str(d)})
            try:
                self._run_hook("DirectoryAdded", payload, matched=str(d))
            except Exception:
                pass

    def _fire_session_end(self, reason: str) -> None:
        """H4 scope B: SessionEnd(quit, /clear). Best-effort/observational
        only -- nothing a SessionEnd hook returns can change anything, the
        session is already ending."""
        if self.hook_runner is None:
            return
        try:
            self.hook_runner.run_session_end(reason)
        except Exception:
            pass  # a SessionEnd hook must never block process/session teardown

    def close_cc(self) -> None:
        """H11 Part B: kill this session's claude subprocess (process
        group) and close its bridge server, if `cc:` was ever used this
        session -- a safe no-op otherwise. Called from Controller.quit(),
        headless.py's own atexit/finally cleanup, and SIGTERM/SIGHUP (via
        the same paths that already call job_registry.kill_all())."""
        from halo_harness.agent import cc_runtime
        cc_runtime.close_cc(self)

    def clear(self) -> None:
        """U5 must-do: `/clear` starts a genuinely NEW session log --
        `SessionEnd(clear)` fired on the OLD one, a fresh `SessionLog`
        (new session_id) built the same way `__init__` builds a brand-new
        session's (meta/system/CLAUDE.md/memory/environment snapshots),
        then `SessionStart(clear)` fired on it -- instead of the old
        "view-only" behaviour (the TUI's own transcript widget was wiped
        but the underlying log/context, and everything the NEXT request
        would still derive from it, was completely untouched). Model/
        settings/permission engine/tool registry are all kept as-is --
        only the CONVERSATION resets, same as Claude Code's own `/clear`.
        Never called while `self.busy` (the caller -- Controller.clear_
        session -- checks first; calling it mid-turn would race the
        worker thread's own log writes)."""
        # H11b finding 8: a live `cc:` claude subprocess holds the WHOLE
        # old conversation -- closing it (and dropping `_cc_state`) here,
        # BEFORE the fresh log below is even built, is what makes the next
        # `cc:` turn's own `ensure_cc_state` correctly see "no cc_session_id
        # meta node in this (brand new) log" and start a genuinely fresh
        # `--session-id`, instead of either reusing the old process (still
        # talking about the pre-/clear conversation) or restarting with
        # `--resume uuid5(<new session id>)` -- an id claude never created,
        # which fails outright ("No conversation found").
        if getattr(self, "_cc_state", None) is not None:
            from halo_harness.agent import cc_runtime
            cc_runtime.close_cc(self)
            self._cc_state = None
        self._fire_session_end("clear")
        self._reset_prune_state()  # H5b finding 2: no old log left for these ids to refer to
        self.log = SessionLog(self.cwd)
        if self.hook_runner is not None:
            # H5c finding 11: update EVERY session-keyed value the hook
            # runner carries BEFORE firing SessionStart(clear) below --
            # otherwise the hook's own payload (session_id/transcript_path)
            # AND the CLAUDE_ENV_FILE path a command hook is launched with
            # (hooks.py's `env_file_path(self.session_id)`, derived from
            # THIS attribute) both still name the OLD session. A clear
            # hook's own `export FOO=bar` then lands in a file nobody ever
            # reads back -- `_fire_session_start`'s own post-hook re-read a
            # few lines below already correctly uses the FRESH
            # `self.log.session_id`, but that only helps once the hook
            # itself was actually told to write to the SAME file.
            self.hook_runner.session_id = self.log.session_id
            self.hook_runner.transcript_path = str(self.log.path)
        self.turn_count = 0
        # W3b item 11: a fresh conversation starts a fresh timeline -- a
        # turn from the conversation /clear just dropped has no business
        # appearing in this (same Session object's) own /timeline any more.
        self._timeline.reset()
        self._last_prompt_tokens = None
        self._just_compacted = False
        self._compaction_backoff_remaining = 0
        self.cost_meter = CostMeter(price_in=self.cost_meter.price_in, price_out=self.cost_meter.price_out,
                                     price_cache_read=self.cost_meter.price_cache_read,
                                     price_cache_write=self.cost_meter.price_cache_write)
        self.permission_denials = []

        initial_tools = (self.tool_registry.definitions_for(self.session_catalog.names)
                          if self.session_catalog is not None else self.tool_registry.definitions())
        self.log.append_meta(
            model=self.model_ref.raw, cwd=str(self.cwd),
            system_prompt_bytes=len(self.session_context.system_prompt.encode("utf-8")),
            tools=initial_tools,
        )
        self.log.append_system(self.session_context.system_prompt)
        claude_md = self.session_context.claude_md_text()
        if claude_md:
            self.log.append_snapshot([{"type": "text", "text": claude_md}], kind="claude_md")
            self._fire_instructions_loaded(claude_md)
        memory_text = self.session_context.memory_snapshot_text()
        if memory_text:
            self.log.append_snapshot([{"type": "text", "text": memory_text}], kind="memory_index")
        env_text = self.session_context.environment_snapshot_text(self.model_label)
        self.log.append_snapshot([{"type": "text", "text": env_text}], kind="environment")
        if self.session_catalog is not None and self.session_catalog.deferred:
            deferred_names = sorted(self.session_catalog.deferred)
            reminder = (
                "The following deferred tools are now available via ToolSearch. Their schemas "
                "are NOT loaded -- calling them directly will fail with InputValidationError. Use "
                "ToolSearch with query \"select:<name>[,<name>...]\" to load tool schemas before "
                "calling them:\n" + "\n".join(deferred_names)
            )
            self.log.append_snapshot([{"type": "text", "text": reminder}], kind="deferred_tools")
        self._fire_session_start("clear")

    def _call_model_for_hook(self, prompt_text: str, timeout_s: float) -> str:
        """The `prompt`/`agent` hook handler types' "small model via the
        provider layer" call [D-CFG]. Uses `self.small_model_ref` when
        configured, else falls back to the main model -- true cross-
        provider small-model routing (separate creds/profile resolution)
        is a follow-up refinement; this already gives every hook a real
        model call today. Thin wrapper around `call_small_model` (H10 Part
        B: generalized so `halo_harness.improve.draft`'s ONE drafting call
        can reuse the SAME one-shot, never-logged, mock-interceptable
        plumbing with its own system prompt/model/token budget instead of
        this method's fixed JSON-verdict shape)."""
        return self.call_small_model(
            system_text="Reply with ONLY a single JSON object {\"ok\": true|false, \"reason\": \"...\"} -- no prose.",
            user_text=prompt_text, max_tokens=min(1024, self.model_profile.max_output_tokens or 1024),
            timeout_s=timeout_s,
        )

    def call_small_model(self, *, system_text: str, user_text: str, max_tokens: int = 4096,
                          timeout_s: float = 60.0, model_ref: Optional[ModelRef] = None) -> str:
        """A ONE-SHOT request built directly via `build_request_body`
        (never through `derive_request`/the session log, so this call is
        NEVER part of the logged conversation) -- shared by
        `_call_model_for_hook` (title generation, prompt-hook verdicts) and
        `halo_harness.improve.draft`'s own drafting call. `model_ref`
        defaults to `self.small_model_ref or self.model_ref` (unchanged
        behavior for every existing caller); a caller that resolved its
        OWN model (e.g. `improve.model` from config) passes it explicitly.

        H11b finding 22: `ref.provider == "cc"` (no distinct non-cc small-
        model configured) can't go through `build_request_body`/
        `stream_completion` at all -- there is no HTTP endpoint for "cc",
        the installed `claude` binary is the provider -- so title
        generation, prompt/agent hooks and `/improve`'s drafting call used
        to fail outright on a `cc:` session ("the summarisation call
        failed"). Routed to a quick, stateless one-shot `claude -p`
        instead (never touches this session's own live `_cc_state`/
        conversation)."""
        ref = model_ref or self.small_model_ref or self.model_ref
        if ref.provider == "cc":
            from halo_harness.agent.cc_runtime import one_shot_cc_call
            return one_shot_cc_call(ref.model, system_text, user_text, timeout_s=timeout_s)
        route = Route(provider=ref.provider, upstream_model=ref.model, dialect=ref.dialect) \
            if ref is not self.model_ref else self.route
        profile = resolve_profile(route) if ref is not self.model_ref else self.provider_profile
        body = build_request_body(
            system_text=system_text,
            messages=[{"role": "user", "content": [{"type": "text", "text": user_text}]}],
            tools=[], route=route, profile=profile, effort=self.effort,
            context_tokens=self.model_profile.context_tokens,
            prompt_estimate=_rough_estimate("", []),
            requested_max_tokens=max_tokens,
        )
        req = self._build_request(body)
        abort = threading.Event()
        timer = threading.Timer(max(0.1, timeout_s), abort.set)
        timer.daemon = True
        timer.start()
        text_parts: list = []
        gen = stream_completion(req, abort=abort)
        try:
            for ev in gen:
                kind = ev.get("type")
                if kind == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text_parts.append(str(delta.get("text", "")))
                elif kind == "error":
                    raise RuntimeError((ev.get("error") or {}).get("message", "model call failed"))
        finally:
            timer.cancel()
            gen.close()
        return "".join(text_parts)

    def _pruned_messages_for_wire(self, raw_messages: list) -> list:
        """H5b finding 2: the STABLE half of tool-result pruning. Computes
        the raw candidate set fresh (cheap: pure token math, no mutation),
        merges any NEWLY-qualifying tool_use_ids into the session's pending
        batch, and only COMMITS the batch (making it part of
        `self._prune_committed_stub_ids`, which every request from here on
        stubs identically) once it is worth at least
        `PRUNE_REBALANCE_CHUNK_TOKENS` -- so the wire prefix up to the
        oldest still-growing content stays byte-identical across every
        request in between two commits, instead of drifting by about one
        message per step. `raw_messages` must be the UNPRUNED
        `derive_request` output -- never a previously-pruned copy (an
        already-stubbed result's short excerpt would under-count its own
        real size on every later call)."""
        candidates = compute_stub_candidates(raw_messages, protect_tokens=PRUNE_PROTECT_TOKENS)
        for tool_use_id, tokens in candidates.items():
            if tool_use_id not in self._prune_committed_stub_ids:
                self._prune_pending_stub_tokens[tool_use_id] = tokens
        # A committed id that no longer appears as a candidate (compaction
        # rewrote the log out from under it) is harmless to keep around --
        # `prune_messages` only ever stubs an id it actually finds a
        # tool_result block for -- but drop it from PENDING so a stale
        # entry can never inflate the chunk-size check below.
        for tool_use_id in list(self._prune_pending_stub_tokens):
            if tool_use_id not in candidates:
                del self._prune_pending_stub_tokens[tool_use_id]
        if sum(self._prune_pending_stub_tokens.values()) >= PRUNE_REBALANCE_CHUNK_TOKENS:
            # H9 whole-tree review finding 28: log the commit BEFORE
            # clearing pending, so a resumed process can rebuild
            # `self._prune_committed_stub_ids` from the log alone -- see
            # `append_prune_commit`'s own docstring. Only the just-added
            # batch (this call's own `_prune_pending_stub_tokens`) is
            # logged, never the whole running set.
            self.log.append_prune_commit(list(self._prune_pending_stub_tokens))
            self._prune_committed_stub_ids |= set(self._prune_pending_stub_tokens)
            self._prune_pending_stub_tokens = {}
        return prune_messages(raw_messages, protect_tokens=PRUNE_PROTECT_TOKENS,
                               force_stub_ids=frozenset(self._prune_committed_stub_ids))

    def _reset_prune_state(self) -> None:
        """Called after `/clear` and after a successful compaction (both
        already discard/rewrite the history any committed/pending id refers
        to) -- starting the batch fresh is correct and safe, never a
        stability regression (there is no OLDER request sharing a prefix
        with the post-clear/post-compaction log to begin with)."""
        self._prune_committed_stub_ids = set()
        self._prune_pending_stub_tokens = {}

    def _on_catalog_grow(self, names: list) -> None:
        """H3 scope C: `SessionCatalog.load()`'s own callback -- a
        ToolSearch call just appended one or more deferred MCP tools to
        the session's wire catalog (or LRU-evicted one back out). Logs a
        NEW `meta` node carrying the full, still name-order-frozen-then-
        append-only tool list (`derive_request` always takes the LAST
        meta node's `tools`, so this is the only write needed for the
        NEXT request to carry it -- see agent/derive.py).

        H11b finding 6: also tells a live `cc:` bridge (a no-op for every
        other route -- `notify_catalog_changed` itself no-ops with no
        `_cc_state`) so its child sends Claude Code a real
        `notifications/tools/list_changed` -- without this, a tool
        ToolSearch loads mid-session enters rolo's own catalog but
        Claude Code, which only ever listed tools once at startup, never
        learns it exists and can never call it."""
        self.log.append_meta(tools=self.tool_registry.definitions_for(names))
        from halo_harness.agent import cc_runtime
        cc_runtime.notify_catalog_changed(self)

    # ---- request construction ------------------------------------------

    def _derive_and_build(self, tool_choice=None, no_tools: bool = False):
        # finding 4: tools=None makes derive_request fall back to the
        # logged meta node's FROZEN catalog, never the live registry --
        # what actually reached the model must match what a later replay
        # reconstructs, independent of whether the registry's tool
        # descriptions changed between this run and a resumed one.
        # H5 scope F item 9: `no_tools=True` (the MAX_STEPS_PROMPT wrap-up
        # call) passes `tools=[]` explicitly -- an EMPTY list, not None, so
        # derive_request never falls back to the frozen catalog either --
        # backing "Tools are disabled until next user input" with an
        # actually-empty wire `tools` field, not just prompt text the model
        # could ignore.
        system_text, messages, tools = derive_request(self.log, tools=([] if no_tools else None))
        # finding 4: `agent/prune.py` used to be wired ONLY into `/context`
        # (its "pruned ~N tokens" report never actually shrank anything a
        # real request sent) -- applied here, AFTER derive_request and
        # BEFORE the wire body is built, so every ordinary step benefits
        # from OpenCode's protection-window pruning, not just an auto/
        # manual/overflow compaction pass. The LOGGED transcript is
        # untouched (prune_messages never mutates its input and this
        # session never writes the pruned copy back to the log). H5b
        # finding 2: `_pruned_messages_for_wire` (not a bare `prune_messages`
        # call) is what keeps the stub boundary STABLE across requests --
        # see its own docstring.
        messages = self._pruned_messages_for_wire(messages)
        requested_max_tokens = max_tokens_budget(
            self.provider_profile, model_key=self.model_ref.raw, requested=None, abort=self.abort,
        )
        # H5 scope C: a native-Anthropic-dialect route (`ant:`, Databricks
        # Claude passthrough) builds an Anthropic Messages body directly --
        # `messages`/`tools` are ALREADY Anthropic-shaped (agent/derive.py's
        # canonical form), so this is assembly, not the openai-chat
        # translation `build_request_body` does.
        if self.route.dialect == "anthropic-passthrough":
            body = build_anthropic_request_body(
                system_text=system_text, messages=messages, tools=tools, route=self.route,
                profile=self.provider_profile, effort=self.effort,
                requested_max_tokens=requested_max_tokens, tool_choice=tool_choice,
            )
        else:
            body = build_request_body(
                system_text=system_text, messages=messages, tools=tools, route=self.route,
                profile=self.provider_profile, effort=self.effort,
                context_tokens=self.model_profile.context_tokens,
                prompt_estimate=_rough_estimate(system_text, messages),
                requested_max_tokens=requested_max_tokens, tool_choice=tool_choice,
            )
        return system_text, messages, tools, body

    def _build_request(self, body: dict) -> CompletionRequest:
        kimi_tool_id_start = 0
        if self.provider_profile.tool_id_format == "kimi_functions_idx":
            # H9 critical review finding 1: seed the per-stream rename
            # counter from the highest `functions.{name}:{idx}` already
            # logged anywhere this session, never 0 -- see
            # agent/invariants.highest_kimi_functions_idx's own docstring.
            from halo_harness.agent.invariants import highest_kimi_functions_idx
            kimi_tool_id_start = highest_kimi_functions_idx(self.log) + 1
        kwargs = dict(
            body={"messages": []}, route=self.route,
            profile={"context_tokens": self.model_profile.context_tokens,
                     "max_output_tokens": self.model_profile.max_output_tokens},
            creds=self.creds, state_dir=self.state_dir, extra_headers=self.extra_headers,
            model_label=self.model_ref.raw, openrouter_base_url=self.openrouter_base_url,
            harness_mode=True, ping_interval=float(env_compat("PING_INTERVAL", default="15")),
            tool_id_format=self.provider_profile.tool_id_format,
            kimi_tool_id_start=kimi_tool_id_start,
        )
        if self.route.dialect == "anthropic-passthrough":
            kwargs["prebuilt_anthropic_body"] = body
        else:
            kwargs["prebuilt_oai_body"] = body
        return CompletionRequest(**kwargs)

    def _stream(self, req: CompletionRequest, abort: "threading.Event | None" = None) -> Iterator[dict]:
        """Dispatch to the right dialect's orchestration -- the ONE place
        that decides `stream_completion` vs `stream_anthropic_completion`,
        so every `_step`/`_run_compaction` call site stays dialect-blind.
        `abort`, when given, is used INSTEAD of `self.abort` -- Halo 2.0.1's
        steer-restart watcher (`_step`) passes a combined `_EitherAbort`
        (this session's real abort OR a dedicated per-attempt restart
        event) so a silently-blocked call can be force-closed without ever
        touching `self.abort` itself (reserved for a genuine user
        interrupt -- see `_EitherAbort`'s own docstring)."""
        eff_abort = abort if abort is not None else self.abort
        if self.route.dialect == "anthropic-passthrough":
            return stream_anthropic_completion(req, abort=eff_abort)
        return stream_completion(req, abort=eff_abort)

    def _abort_sleep(self, delay: float) -> bool:
        """Sleep up to `delay` seconds in short increments, checking
        `self.abort` between each (must-do 5: a retry wait must be
        interruptible, never a flat blocking `time.sleep`). Returns True
        if the abort fired before the delay elapsed (the caller must stop,
        never retry)."""
        deadline = time.monotonic() + delay
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self.abort.is_set():
                return True
            time.sleep(min(0.25, remaining))

    # ---- one model call --------------------------------------------------

    def _step(self, turn_no: int, tool_choice=None, overflow_handled: bool = False, no_tools: bool = False):
        """One model call, streamed: yields translated Events AS EACH wire
        event arrives (never buffers the whole reply before yielding
        anything) and `return`s a `_StepResult` (retrieved by the caller
        via `result = yield from self._step(...)`), or `None` if a
        terminal failure already emitted its own `error` event (the caller
        must then stop the turn, never commit a partial assistant
        message). A retryable upstream failure gets up to `MAX_RETRIES`
        retries on the 1-2-4-8-16s ladder (finding 7), abort-aware; a
        DeepSeek reasoning-replay 400 is a request-builder BUG, surfaced
        immediately on EITHER phase, never retried; an empty completion
        (finding 7) and a `length` reply with <=1 output token
        (finding 3) are each a provider failure, not retried in place.
        `tool_choice` (H2 scope B: the repair layer's own one-shot
        `tool_choice: required` retry, called from `_turn_body` -- never
        set on an ordinary call) is forwarded straight to
        `build_request_body`, which downgrades it to "auto" itself for any
        profile row that doesn't support "required" (DeepSeek thinking/
        GLM/Qwen -- `_turn_body` also checks this UP FRONT so it never even
        calls `_step` with "required" for those rows, but the downgrade
        stays as a second, cheap line of defense)."""
        try:
            _, _, _, body = self._derive_and_build(tool_choice=tool_choice, no_tools=no_tools)
        except ToolCatalogTooLarge as e:
            # must-do 4: today this escaped as a bare traceback.
            yield events.error(
                f"{e} -- this provider caps tool catalogs at {e.limit}; reduce the number of tools offered",
                turn=turn_no, err_type="tool_catalog_too_large",
            )
            return None
        req = self._build_request(body)

        attempts = 0
        empty_retried = False
        # 1.0.1 fixpass finding 10: TWO independent one-shot flags, not one
        # shared `effort_retried` -- the gpt-6-with-tools repair
        # (reasoning_effort:"none") and the general strip-the-field repair
        # are DIFFERENT fixes for different messages; sharing one flag meant
        # a "none" retry that itself still failed (still invalid on that
        # route) permanently blocked the strip retry from ever running,
        # failing the turn outright when stripping the field would have
        # worked. Each may now fire once, in order, on separate failures of
        # the SAME step.
        effort_none_retried = False
        effort_stripped_retried = False
        # H10 Part A: `call_t0` starts once, before the FIRST attempt --
        # `latency_ms` on a call that only succeeded after a retry ladder
        # (429/5xx backoff) reports the user-visible wall-clock time for
        # the whole turn's model call, not just its final attempt.
        # `ttft_ms` is set the first time ANY content actually streams
        # (text/thinking/tool_use), on whichever attempt that turns out to
        # be; stays None for a reply with no streamed content at all
        # (an immediate tool_use with no preceding text still counts, via
        # the `content_block_start` branch below).
        call_t0 = time.monotonic()
        ttft_ms: Optional[float] = None
        # Halo 2.0.1 W2a telemetry (GLM-brief.md item 6 / liveness-tips-
        # brief Part A6): same "measured from call_t0, set on whichever
        # attempt gets there first" contract as `ttft_ms` above.
        ttfb_ms: Optional[float] = None
        first_text_ms: Optional[float] = None
        first_tool_ms: Optional[float] = None
        first_reasoning_ms: Optional[float] = None
        native_thinking_delta_count = 0
        restart_when_silent = _steer_restart_when_silent_enabled()
        while True:
            attempts += 1
            # HALO-2.0.1-liveness-tips-brief.md Part A1 / W2-plan item 6:
            # one `phase` event per transition the UI renders a live line
            # from -- "request_sent" fires on every attempt (a retry or a
            # steer-restart both genuinely send a fresh request).
            yield events.phase(state="request_sent", turn=turn_no, model=self.model_ref.raw)
            # GLM-brief.md item 3 / W2-plan item 2: "when a steer arrives
            # and no chunk has been received for the in-flight call, abort
            # that call and resend with the steer appended" -- the existing
            # `_pending_steer()` check just below the `for ev in gen:` line
            # already cuts a call AFTER its first chunk; this watcher covers
            # the case nothing has arrived yet (the generator is blocked
            # inside a real connect/socket read, so THIS thread can't poll
            # anything until it unblocks). `chunk_started` flips True at the
            # SAME point `ttft_ms` is set below; `steer_restart_event` is a
            # DEDICATED event (never `self.abort` -- see `_EitherAbort`'s
            # own docstring for why) that `providers/stream.py`'s existing
            # abort-polling (already passed whatever `abort=` `_stream`
            # receives) force-closes the connection for, within one ~0.2s
            # poll tick, exactly like a real interrupt would.
            chunk_started = [False]
            steer_restart_event = threading.Event()
            watcher_stop = threading.Event()
            watcher = None
            if restart_when_silent:
                def _watch_for_silent_steer(_stop=watcher_stop, _started=chunk_started, _fire=steer_restart_event):
                    while not _stop.is_set():
                        if not _started[0] and self._pending_steer():
                            _fire.set()
                            return
                        _stop.wait(0.2)
                watcher = threading.Thread(target=_watch_for_silent_steer, daemon=True)
                watcher.start()
            call_abort = _EitherAbort(self.abort, steer_restart_event) if restart_when_silent else self.abort
            gen = self._stream(req, abort=call_abort)
            assistant_blocks: list = []
            partial_json: dict = {}
            stop_reason = None
            usage: dict = {}
            harness_meta: dict = {}
            wire_error: Optional[dict] = None
            phase1_failure: Optional[Exception] = None
            steered_cut = False
            try:
                for ev in gen:
                    if self._pending_steer():
                        # scope 0(c): "the in-flight model generation is
                        # cut at the next chunk" -- checked BEFORE this
                        # chunk is applied, so nothing arriving after the
                        # steer was queued ever reaches the log/UI. Any
                        # tool_use block still mid-formation (never got its
                        # own content_block_stop) is dropped below, once
                        # the loop exits -- only fully-formed blocks (text/
                        # thinking so far, or a tool_use that JUST closed)
                        # survive into the result.
                        steered_cut = True
                        break
                    kind = ev.get("type")
                    if ttfb_ms is None:
                        # Halo 2.0.1: the FIRST event of ANY kind -- for the
                        # chat dialect this is the synthesized `message_start`
                        # (yielded the instant phase 1 gets a 2xx, before any
                        # real body byte is read -- see oai_stream.py's
                        # `message_start_event`), for native Anthropic it's
                        # the real wire `message_start`, sent essentially
                        # immediately. Either way this is "headers arrived".
                        ttfb_ms = round((time.monotonic() - call_t0) * 1000, 1)
                        yield events.phase(state="headers", turn=turn_no, ttfb_ms=ttfb_ms)
                    elif kind == "reasoning_started":
                        # Halo 2.0.1: oai_stream.py's own real-time marker --
                        # the chat dialect never emits a translated event for
                        # reasoning deltas themselves (captured silently,
                        # displayed as one lump once the stream ends), so
                        # without this signal `first_token` would wrongly
                        # fire on whatever REAL content (text/tool) streams
                        # next instead of the reasoning that genuinely
                        # arrived first on the wire.
                        if first_reasoning_ms is None:
                            first_reasoning_ms = round((time.monotonic() - call_t0) * 1000, 1)
                        if not chunk_started[0]:
                            chunk_started[0] = True
                            yield events.phase(state="first_token", turn=turn_no, kind="reasoning")
                    elif kind == "tool_started":
                        # Halo 2.0.1: same idea as "reasoning_started" --
                        # oai_stream.py's `content_block_start(tool_use)`
                        # only fires at `_finalize()` (stream end) for the
                        # chat dialect, well after the tool call's arguments
                        # actually started streaming; this marker is the
                        # real-time signal.
                        if first_tool_ms is None:
                            first_tool_ms = round((time.monotonic() - call_t0) * 1000, 1)
                        if not chunk_started[0]:
                            chunk_started[0] = True
                            yield events.phase(state="first_token", turn=turn_no, kind="tool")
                    # H10 Part A: time-to-first-content-block, measured from
                    # `call_t0` (the FIRST attempt) so a call that needed a
                    # 429/5xx retry still reports the real user-visible wait.
                    if ttft_ms is None and kind == "content_block_start":
                        ttft_ms = round((time.monotonic() - call_t0) * 1000, 1)
                        block_kind = (ev.get("content_block") or {}).get("type")
                        token_kind = {"thinking": "reasoning", "tool_use": "tool"}.get(block_kind, "text")
                        if token_kind == "text":
                            first_text_ms = ttft_ms
                        elif token_kind == "tool" and first_tool_ms is None:
                            first_tool_ms = ttft_ms  # native-Anthropic path (no "tool_started" marker there)
                        elif token_kind == "reasoning" and first_reasoning_ms is None:
                            first_reasoning_ms = ttft_ms  # native Anthropic thinking -- a real wire block opened
                        # Halo 2.0.1: `chunk_started[0]` is the SINGLE
                        # authoritative "first_token already fired" flag,
                        # shared with the "reasoning_started"/"tool_started"
                        # real-time markers above -- a native thinking block
                        # that already fired via neither marker (the chat-
                        # dialect-only signals) still only reports
                        # "first_token" once, for whichever kind got here
                        # first chronologically.
                        if not chunk_started[0]:
                            chunk_started[0] = True
                            yield events.phase(state="first_token", turn=turn_no, kind=token_kind)
                    if kind == "message_start":
                        yield events.message_start(turn=turn_no, model=self.model_ref.raw)
                        # finding 15 (major, h4-h5-h3c review), point 3:
                        # Anthropic's OWN initial usage (input_tokens,
                        # cache_creation_input_tokens, cache_read_
                        # input_tokens) arrives ONLY on message_start, not
                        # message_delta (which typically only ever updates
                        # output_tokens) -- ignored entirely before, so a
                        # native-dialect turn's usage/cost/context%
                        # reporting was missing everything but output
                        # tokens. `.update()` here, `.update()` again on
                        # message_delta below: the delta's own later keys
                        # (a growing output_tokens as the reply streams)
                        # correctly win over this initial snapshot.
                        start_usage = (ev.get("message") or {}).get("usage")
                        if isinstance(start_usage, dict):
                            usage.update(start_usage)
                    elif kind == "content_block_start":
                        block = ev.get("content_block") or {}
                        idx = ev.get("index", len(assistant_blocks))
                        while len(assistant_blocks) <= idx:
                            assistant_blocks.append(None)
                        btype = block.get("type")
                        if btype == "text":
                            assistant_blocks[idx] = {"type": "text", "text": ""}
                        elif btype == "thinking":
                            assistant_blocks[idx] = {"type": "thinking", "text": "", "signature": block.get("signature", "")}
                        elif btype == "tool_use":
                            assistant_blocks[idx] = {"type": "tool_use", "id": block.get("id"),
                                                      "name": block.get("name"), "input": {}}
                            partial_json[idx] = ""
                            yield events.Event("tool_use_start", {"id": block.get("id"), "name": block.get("name")}, turn=turn_no)
                    elif kind == "content_block_delta":
                        idx = ev.get("index", 0)
                        delta = ev.get("delta") or {}
                        dtype = delta.get("type")
                        if idx >= len(assistant_blocks) or assistant_blocks[idx] is None:
                            continue
                        if dtype == "text_delta":
                            text = delta.get("text", "")
                            text = text if isinstance(text, str) else str(text)  # never crash on a non-str delta
                            assistant_blocks[idx]["text"] += text
                            yield events.text_delta(text, index=idx, turn=turn_no)
                        elif dtype == "thinking_delta":
                            # finding 15 (major, h4-h5-h3c review), point 1:
                            # the wire key is `thinking`, not `text` --
                            # every native Anthropic thinking_delta event's
                            # actual content was silently dropped (read as
                            # "" every time) before this fix.
                            text = delta.get("thinking", "")
                            text = text if isinstance(text, str) else str(text)
                            assistant_blocks[idx]["text"] += text
                            yield events.thinking_delta(text, index=idx, turn=turn_no)
                            # Halo 2.0.1 W2a telemetry: counts REAL
                            # incremental native-thinking wire deltas --
                            # ">1" feeds `reasoning_streamed` below (the
                            # native Anthropic dialect genuinely streams
                            # reasoning; a count of exactly 0 or 1 means an
                            # empty/trivial "display omitted" block, not
                            # worth calling "streamed").
                            native_thinking_delta_count += 1
                        elif dtype == "signature_delta":
                            assistant_blocks[idx]["signature"] = assistant_blocks[idx].get("signature", "") + str(delta.get("signature", ""))
                        elif dtype == "input_json_delta":
                            partial_json[idx] = partial_json.get(idx, "") + delta.get("partial_json", "")  # ACCUMULATE, never overwrite
                    elif kind == "content_block_stop":
                        idx = ev.get("index", 0)
                        if idx in partial_json and idx < len(assistant_blocks) and assistant_blocks[idx] is not None:
                            raw = partial_json.pop(idx)
                            try:
                                assistant_blocks[idx]["input"] = json.loads(raw) if raw.strip() else {}
                            except json.JSONDecodeError:
                                assistant_blocks[idx]["input"] = {}
                                assistant_blocks[idx]["_raw_unparsed"] = raw
                    elif kind == "message_delta":
                        delta = ev.get("delta") or {}
                        if delta.get("stop_reason") is not None:
                            stop_reason = delta.get("stop_reason")
                        if isinstance(ev.get("usage"), dict):
                            usage.update(ev["usage"])
                        if isinstance(ev.get("harness_meta"), dict):
                            harness_meta = ev["harness_meta"]
                    elif kind == "error":
                        wire_error = ev.get("error") or {}
                        break
                    # "ping"/"message_stop": nothing to translate
            except (ContextOverflow, ProviderNotConfigured, UpstreamError) as e:
                # Phase 1 (translate + connect, before anything is yielded)
                # raises these as plain Python exceptions, not wire "error"
                # events -- caught here so they get the SAME retry/terminal
                # handling as a phase-2 wire error below.
                phase1_failure = e
            finally:
                gen.close()  # always close the upstream generator
                watcher_stop.set()
                if watcher is not None:
                    watcher.join(timeout=0.5)

            if steer_restart_event.is_set() and not self.abort.is_set():
                # GLM-brief.md item 3: the watcher above force-closed this
                # silently-blocked call -- `gen` is therefore already fully
                # drained (stream.py's own `_Aborted`/abort-polling returns
                # a cleanly-exhausted generator, exactly like a real
                # interrupt would, but `self.abort` itself was never
                # touched -- see `_EitherAbort`). Apply the queued steer(s)
                # NOW (the same machinery an ordinary mid-stream steer
                # uses) and rebuild the request from the updated log before
                # retrying -- never counted against `effort_*_retried`'s
                # one-shot flags or any retry-ladder cap, since nothing
                # here was a provider failure.
                with self._steer_lock:
                    pending_preview = list(self._steer_queue)
                applied = yield from self._apply_pending_steers_events(turn_no)
                if applied:
                    yield events.steer_restart(", ".join(pending_preview), turn=turn_no)
                _, _, _, body = self._derive_and_build(tool_choice=tool_choice, no_tools=no_tools)
                req = self._build_request(body)
                # Halo 2.0.1: unlike an ordinary 429/5xx retry (where
                # `ttfb_ms` correctly keeps measuring "time since call_t0
                # to the first attempt that got anywhere"), a steer-restart
                # deliberately THROWS AWAY an attempt that already passed
                # headers (that's the whole point -- it was silent AFTER
                # them) -- reset so the retried attempt gets its own fresh
                # "headers" phase event instead of the UI skipping straight
                # from "sending" to "first token" with no transition in
                # between. `first_text_ms`/`first_tool_ms`/`first_
                # reasoning_ms` need no such reset: the restart only ever
                # fires while `chunk_started[0]` is still False, i.e. before
                # any of the three could have been set.
                ttfb_ms = None
                continue

            if self.abort.is_set():
                # U2/review finding 2: an interrupt (Esc/Ctrl+C in the
                # TUI, or a UI `quit`) cuts this call short -- never
                # retried, never dressed up as a provider failure;
                # `_turn_body` reports it as `turn_done(reason=
                # "interrupted")`. The old behaviour silently DROPPED
                # whatever text/thinking had already streamed -- logged
                # here instead, so the transcript/model both see exactly
                # what was cut off rather than a gap.
                partial = [b for b in assistant_blocks if b is not None and b.get("type") in ("text", "thinking")]
                for b in partial:
                    if isinstance(b.get("text"), str):
                        b["text"] = repair_truncated_text(b["text"])
                if partial:
                    partial.append({"type": "text", "text": "[Request interrupted by user]"})
                    self.log.append_assistant(content=partial, stop_reason="interrupted")
                self._log_call_failure("aborted", retries=attempts - 1)
                return None

            if steered_cut:
                # scope 0(c): the stream was cut for a queued steer, not
                # an error -- any tool_use block that never reached its
                # own content_block_stop (input never parsed) is dropped;
                # a block that DID already close (rare, but the steer may
                # have been noticed on the very next chunk after one
                # finished) still SURVIVES into the logged assistant
                # message (it must be, to keep wire pairing valid -- see
                # `_assistant_block_is_replayable`), but H5c finding 6:
                # `_dispatch_tools`' own top-of-loop `_pending_steer()`
                # check now stops it from ever being DISPATCHED -- "running
                # tools allowed to finish" never applied to it in the first
                # place, since `dispatch()` was never actually called for
                # it (it hadn't started, just finished FORMING). Falls
                # straight through to a normal, un-retried result (never
                # the empty-completion-retry path below -- an intentional
                # cut is not a provider failure).
                for idx in list(partial_json.keys()):
                    if idx < len(assistant_blocks):
                        assistant_blocks[idx] = None
                break

            if phase1_failure is not None:
                if isinstance(phase1_failure, ContextOverflow):
                    e = phase1_failure
                    if not overflow_handled:
                        # H5 scope B: signal the caller (_turn_body) to run
                        # one compaction pass and retry this SAME step once
                        # -- `overflow_handled=True` on that retry means a
                        # SECOND overflow falls through to the terminal
                        # error below instead of looping forever.
                        return _OVERFLOW_NEEDS_COMPACTION
                    self._log_call_failure("overflow", retries=attempts - 1)
                    yield events.error(
                        f"context window overflow (limit={e.limit} tokens, prompt~={e.prompt_tokens}) -- "
                        f"compaction did not free enough room; try `/compact <instructions>` to focus the "
                        f"summary, or start a new session",
                        turn=turn_no, err_type="context_overflow", category=CONTEXT_WINDOW_EXCEEDED,
                    )
                    return None
                if isinstance(phase1_failure, ProviderNotConfigured):
                    yield events.error(str(phase1_failure), turn=turn_no, err_type="not_configured")
                    return None
                e = phase1_failure  # UpstreamError
                if is_reasoning_replay_bug(e.message):
                    # finding 7: a direct phase-1 400 (Databricks/DeepSeek)
                    # is checked too, not just a phase-2 wire error -- this
                    # ALWAYS means OUR OWN reasoning-replay logic has a bug.
                    yield events.error(f"reasoning-replay bug (never retried): {e.message}", turn=turn_no, err_type="reasoning_replay_bug")
                    return None
                # 1.0.1 hotfix 19.3: an effort-field 400 is a phase-1
                # failure too, not a phase-2 (mid-stream) wire_error -- a
                # non-2xx HTTP response is rejected before any SSE ever
                # starts, so it's ALWAYS raised as this UpstreamError, never
                # reaches the `wire_error is not None` branch below (which
                # still carries its own COPY of this same check, as a
                # backstop for any dialect that somehow surfaces it
                # mid-stream instead). See that branch's own comment for
                # the full rationale.
                if is_effort_with_tools_rejected_message(e.message) and not effort_none_retried:
                    # 1.0.1 hotfix 22: the gpt-6-family-specific wording --
                    # dropping the field is not enough (the endpoint's own
                    # default is not none either), so this sets it
                    # explicitly instead of stripping it.
                    effort_none_retried = True
                    log.warning("reasoning_effort+tools rejected by upstream (%s) -- retrying once with "
                                "reasoning_effort='none'", e.message)
                    body = {**body, "reasoning_effort": "none"}
                    body.pop("reasoning", None)  # host_specific_fields shape -- never both at once
                    req = self._build_request(body)
                    continue
                if is_effort_rejected_message(e.message) and not effort_stripped_retried:
                    # 1.0.1 fixpass finding 10: reachable even after a failed
                    # "none" retry just above (independent flag) -- e.g. this
                    # message matched BOTH checks and "none" itself 400'd
                    # again; stripping the field entirely is the fallback.
                    effort_stripped_retried = True
                    log.warning("effort field rejected by upstream (%s) -- retrying once with it removed", e.message)
                    body = {k: v for k, v in body.items()
                             if k not in ("thinking", "output_config", "reasoning", "reasoning_effort")}
                    req = self._build_request(body)
                    continue
                # H5 scope F item 2: OpenCode's message-pattern classifier
                # (Appendix B) is OR'd onto the existing status-table
                # decision -- either one saying "retry" is enough; only
                # BOTH saying "don't" stops the ladder. `retry_delay_ms`
                # (Appendix B's jittered 2s*2^n / retry-after-ms / retry-
                # after formula) drives the actual wait, still capped by
                # `_MAX_RETRY_WAIT_S` the same way the old ladder was.
                #
                # 1.0.1 hotfix 2: a connect-phase failure (DNS/refused/
                # unreachable/our own bounded-connect timeout --
                # recognized by `providers.http.is_connect_failure_message`,
                # the canonical "cannot resolve/reach <host> ..." lead-in
                # EVERY connect failure's message carries, whichever wire
                # mapper (`databricks_unreachable_response`/
                # `map_upstream_error`) re-wrapped it -- NEVER rides this
                # ladder, full stop. Checked BEFORE `is_retryable_message`,
                # which would otherwise blanket-retry it anyway (status is
                # always 502, and that function retries any status>=500
                # regardless of message). 2.0.1 finding 18: phase 1
                # (`_run_phase1_attempts`/`_run_phase1_anthropic`) no longer
                # retries a connect-phase failure AT ALL (a retry cannot
                # help DNS) -- this `UpstreamError` is raised straight from
                # phase 1's own first and only attempt, so this is correctly
                # terminal here either way -- retrying a DNS failure on a
                # 1-16s timer just repeats the identical failure, slower.
                if is_connect_failure_message(e.message):
                    self._log_call_failure(self._status_label(e.status), retries=attempts - 1)
                    yield events.error(e.message, turn=turn_no, err_type=e.err_type, retryable=False,
                                        category=overflow_classifier(e.status, e.message))
                    return None
                merged_retryable = e.retryable or is_retryable_message(
                    e.status, e.message, host=self.model_ref.provider,
                )
                if merged_retryable and attempts <= MAX_RETRIES:
                    hdrs = {"retry-after": e.retry_after} if e.retry_after else {}
                    delay = _capped_retry_delay(attempts, hdrs)
                    if self._abort_sleep(delay):
                        return None
                    continue
                # must-do 3: overflow_classifier gives the CALLER a
                # dsh-style taxonomy bucket (AUTH/RATE_LIMIT/CONTEXT_WINDOW_
                # EXCEEDED/PROVIDER_FAILURE/...) alongside the raw
                # per-source wire err_type, so it can react by KIND of
                # failure without parsing vendor-specific strings itself.
                self._log_call_failure(self._status_label(e.status), retries=attempts - 1)
                yield events.error(e.message, turn=turn_no, err_type=e.err_type, retryable=e.retryable,
                                    category=overflow_classifier(e.status, e.message))
                return None

            if wire_error is not None:
                message = wire_error.get("message", "unknown upstream error")
                if is_reasoning_replay_bug(message):
                    yield events.error(f"reasoning-replay bug (never retried): {message}", turn=turn_no, err_type="reasoning_replay_bug")
                    return None
                # 1.0.1 hotfix 19.3: a 400 naming the effort field itself
                # (verified wording: `output_config.effort: Input should be
                # 'low', 'medium', 'high' or 'max'`) retries ONCE with every
                # effort-related field stripped from the body -- omitting it
                # always falls back to the provider's own default on every
                # dialect, so this can only turn a hard failure into a
                # successful turn at a slightly lower effort, never the
                # reverse. `clamp_effort` (providers/profiles.py) already
                # prevents the KNOWN case (`xhigh` on an Anthropic route)
                # from ever reaching here; this is the backstop for a route
                # whose accepted set isn't modeled correctly yet.
                if is_effort_with_tools_rejected_message(message) and not effort_none_retried:
                    effort_none_retried = True
                    log.warning("reasoning_effort+tools rejected by upstream (%s) -- retrying once with "
                                "reasoning_effort='none'", message)
                    body = {**body, "reasoning_effort": "none"}
                    body.pop("reasoning", None)
                    req = self._build_request(body)
                    continue
                if is_effort_rejected_message(message) and not effort_stripped_retried:
                    # 1.0.1 fixpass finding 10: independent flag -- see the
                    # matching UpstreamError branch's own comment above.
                    effort_stripped_retried = True
                    log.warning("effort field rejected by upstream (%s) -- retrying once with it removed", message)
                    body = {k: v for k, v in body.items()
                             if k not in ("thinking", "output_config", "reasoning", "reasoning_effort")}
                    req = self._build_request(body)
                    continue
                retryable = wire_error.get("type") in ("overloaded_error", "rate_limit_error", "api_error")
                merged_retryable = retryable or is_retryable_message(
                    wire_error.get("status"), message, host=self.model_ref.provider,
                )
                if merged_retryable and attempts <= MAX_RETRIES:
                    hdrs = {"retry-after": wire_error.get("retry_after")} if wire_error.get("retry_after") else {}
                    delay = _capped_retry_delay(attempts, hdrs)
                    if self._abort_sleep(delay):
                        return None
                    continue
                # a wire error drops the partial reply -- never committed to the log
                wire_status = _WIRE_ERROR_TYPE_TO_STATUS.get(wire_error.get("type"), 500)
                self._log_call_failure(self._status_label(wire_status), retries=attempts - 1)
                yield events.error(message, turn=turn_no, err_type=wire_error.get("type", "error"),
                                    category=overflow_classifier(wire_status, message))
                return None

            # A clean stream from here on.
            if harness_meta.get("model_context_window_exceeded"):
                # GLM-brief.md item 3 / W2-plan item 3: a chat-dialect
                # gateway can end an otherwise-clean 200 stream with
                # `finish_reason: "model_context_window_exceeded"` --
                # mirrors the phase-1 `ContextOverflow` handling above
                # exactly (same sentinel, same compaction-then-retry-once
                # contract, same terminal wording/category on a SECOND
                # overflow), since this is the identical failure arriving
                # mid-stream instead of as an upfront 400.
                if not overflow_handled:
                    return _OVERFLOW_NEEDS_COMPACTION
                self._log_call_failure("overflow", retries=attempts - 1)
                yield events.error(
                    "context window overflow (reported by the model mid-reply) -- compaction did not "
                    "free enough room; try `/compact <instructions>` to focus the summary, or start a "
                    "new session",
                    turn=turn_no, err_type="context_overflow", category=CONTEXT_WINDOW_EXCEEDED,
                )
                return None
            if harness_meta.get("sensitive_finish"):
                # GLM-brief.md item 3: "sensitive becomes an error event
                # carrying the provider's message" -- a clear, NEVER-RETRIED
                # failure (unlike the overflow/empty-completion cases above,
                # re-running the identical request against a content-policy
                # refusal would just repeat it). `text_so_far` (computed
                # just below, pulled up here too) is whatever the provider
                # DID stream before refusing -- its own explanation, when it
                # sends one as ordinary content, same as a real wire 4xx
                # would have in its JSON body.
                sensitive_text = "".join(
                    b.get("text", "") for b in assistant_blocks if b and b.get("type") == "text") or "(no message)"
                self._log_call_failure("sensitive", retries=attempts - 1)
                yield events.error(
                    f"upstream ended the reply early (finish_reason=sensitive): {sensitive_text}",
                    turn=turn_no, err_type="sensitive",
                )
                return None
            if harness_meta.get("length_with_minimal_output"):
                # finding 3: finish_reason=length with <=1 output token is
                # indistinguishable from a genuine per-endpoint cap -- a
                # provider failure to re-route/re-pin, never retried in place.
                yield events.error(
                    "upstream returned finish_reason=length with essentially no output "
                    "(provider failure -- try a lower --effort or a different model/pin)",
                    turn=turn_no, err_type="provider_failure",
                )
                return None

            text_so_far = "".join(b.get("text", "") for b in assistant_blocks if b and b.get("type") == "text")
            tool_use_count = sum(1 for b in assistant_blocks if b and b.get("type") == "tool_use")
            if is_retryable_empty_completion(stop_reason=stop_reason, text=text_so_far, tool_use_count=tool_use_count,
                                              tools_present=bool(body.get("tools"))):
                if not empty_retried:
                    empty_retried = True
                    continue
                yield events.error(
                    "upstream returned an empty completion twice in a row "
                    "(provider failure -- try a lower --effort or a different model/pin)",
                    turn=turn_no, err_type="provider_failure",
                )
                return None
            if effort_none_retried and not effort_stripped_retried and self.model_ref.provider == "databricks":
                # 1.0.1 part 2 fixpass finding 13: `not effort_stripped_
                # retried` -- without it, this block also fired when the
                # "none" retry ITSELF then 400'd again and the SEPARATE
                # strip-the-field retry is what actually succeeded, wrongly
                # learning "none" as this endpoint's permanent rule even
                # though "none" was already proven rejected earlier in this
                # exact retry ladder. Every later session against this
                # endpoint then paid one guaranteed-to-fail request per
                # tool step (the learned "none" value, retried with it
                # stripped every time) and `/effort` showed "none (tools)"
                # for a rule that was never really true.
                # item 22 remainder: this endpoint rejects reasoning_effort
                # alongside tools and a retry with it forced to "none" just
                # succeeded -- remember it for the REST OF THIS SESSION (so
                # every later step sends it correctly the first time,
                # instead of paying for the same failing request again)
                # and in the per-endpoint cache on disk (so the NEXT
                # session against this same endpoint never has to re-learn
                # it live either). A model_table.json row already covering
                # this endpoint means `self.provider_profile.reasoning_
                # effort_with_tools` is already "none" here -- the dataclasses.
                # replace below is then a harmless no-op (same value in,
                # same value out) and the disk write short-circuits too
                # (learn_reasoning_effort_with_tools only ever writes on a
                # real change).
                if self.provider_profile.reasoning_effort_with_tools != "none":
                    self.provider_profile = dataclasses.replace(
                        self.provider_profile, reasoning_effort_with_tools="none")
                from halo_harness.providers.learned_rules import learn_reasoning_effort_with_tools
                learn_reasoning_effort_with_tools(self.state_dir, "databricks", self.model_ref.model, "none")
            if effort_stripped_retried and self.effort is not None:
                # 1.0.1 fixpass finding 10: a successful strip-the-field
                # retry proves THIS session's `self.effort` value is
                # rejected outright on this route -- reset it (rather than
                # leaving it set to the value that just failed) so every
                # LATER step's own first attempt builds its request with no
                # effort field at all, instead of repeating the SAME
                # rejected value first and paying for two requests per step
                # for the rest of the session.
                log.info("effort '%s' rejected on this route -- cleared for the rest of the session "
                         "after a successful strip retry", self.effort)
                self.effort = None
            break  # a clean, non-empty stream -- proceed to build the result

        reasoning = None
        if harness_meta.get("reasoning_text") or harness_meta.get("reasoning_details"):
            # H2: {"text","details"} carries BOTH pieces (finding 2's second
            # bug -- the old {"format","value"} shape could only ever carry
            # one), so hooks.reasoning_echo can replay whichever (or both,
            # for a dual-field DeepSeek V4 row) the profile needs.
            reasoning = {
                "text": repair_truncated_text(harness_meta.get("reasoning_text") or ""),
                "details": harness_meta.get("reasoning_details"),
            }
            # finding 15: captured OpenAI-dialect reasoning (DeepSeek/Kimi/
            # GLM/... reasoning_content, or OpenRouter reasoning_details)
            # never became a thinking_delta event before -- oai_stream.py
            # only ever surfaces it at stream END (harness_meta), never as
            # incremental wire deltas the way native Anthropic thinking
            # does, so this is necessarily ONE synthetic event carrying the
            # whole captured text, not a true incremental stream; output.py
            # buffers-then-flushes per message anyway (see its own
            # docstring), so this is display-equivalent to a real delta.
            display_text = reasoning["text"] or _reasoning_details_display_text(harness_meta.get("reasoning_details"))
            if display_text:
                yield events.thinking_delta(display_text, index=0, turn=turn_no)
            # Halo 2.0.1 W2a telemetry: a belt-and-suspenders fallback for a
            # reasoning shape that somehow never fired oai_stream.py's own
            # real-time "reasoning_started" marker (every shape that module
            # currently recognizes does -- see its own `_note_reasoning_
            # chunk` call sites) -- `first_reasoning_ms`/the `first_token`
            # phase event must still appear rather than silently missing.
            if first_reasoning_ms is None:
                first_reasoning_ms = round((time.monotonic() - call_t0) * 1000, 1)
            if not chunk_started[0]:
                chunk_started[0] = True
                yield events.phase(state="first_token", turn=turn_no, kind="reasoning")
        # ">1" wire chunks (oai_stream.py's own `reasoning_chunk_count`) OR
        # multiple real native `thinking_delta` events both mean "this
        # gateway genuinely streamed reasoning incrementally" -- see
        # `OpenAIStreamToAnthropic._note_reasoning_chunk`'s own docstring
        # for why rolo's real session logs need this to answer GLM-brief.md
        # item 6's open question (does Databricks stream GLM's reasoning?).
        reasoning_streamed = (native_thinking_delta_count > 1) or (
            (harness_meta.get("reasoning_chunk_count") or 0) > 1)
        cleaned = []
        for b in assistant_blocks:
            if b is None:
                continue
            if b.get("type") in ("text", "thinking") and isinstance(b.get("text"), str):
                b["text"] = repair_truncated_text(b["text"])
            cleaned.append(b)
        return _StepResult(assistant_blocks=cleaned, stop_reason=stop_reason, usage=usage, reasoning=reasoning,
                            body=body, tool_call_flags=harness_meta.get("tool_call_flags") or {},
                            latency_ms=round((time.monotonic() - call_t0) * 1000, 1), ttft_ms=ttft_ms,
                            retries=attempts - 1, status="ok",
                            responding_provider=harness_meta.get("responding_provider"),
                            ttfb_ms=ttfb_ms, first_reasoning_ms=first_reasoning_ms, first_text_ms=first_text_ms,
                            first_tool_ms=first_tool_ms, reasoning_streamed=reasoning_streamed)

    # H10 Part A: "or"|"dbx"|"ant" -- the coarse routing rail
    # (`ModelRef.provider`), independent of which specific backend actually
    # answered (that's `_responding_provider_label` below).
    _ROUTE_LABELS = {"openrouter": "or", "databricks": "dbx", "anthropic": "ant"}

    def _responding_provider_label(self, result: "_StepResult") -> str:
        """The RESPONDING provider, per route: OpenRouter's own per-chunk
        `provider` field when captured (falls back to the bare "openrouter"
        label for a reply that never carried one -- a non-streaming/older
        response shape, or a scripted test upstream); the Databricks
        ENDPOINT NAME (the bare upstream model id, Unity Gateway's own
        naming) for a `dbx:` route; the literal "anthropic" for a native
        `ant:` route."""
        if self.model_ref.provider == "openrouter":
            return result.responding_provider or "openrouter"
        if self.model_ref.provider == "databricks":
            return self.model_ref.model
        return "anthropic"

    @staticmethod
    def _status_label(code) -> str:
        """H10 Part A: map an HTTP-ish status code to the `usage.status`
        enum (`429|5xx|connect_error` -- `ok`/`overflow`/`aborted` are set
        by their own call sites directly, never through here)."""
        if code == 429:
            return "429"
        if isinstance(code, int) and 500 <= code < 600:
            return "5xx"
        return "connect_error"

    def _log_call_failure(self, status: str, *, retries: int = 0) -> None:
        """H10 Part A: a model call that never produced a `_StepResult` at
        all (every retry exhausted, or a non-retryable failure) previously
        left NO trace in the session log -- `_account_usage` is only ever
        called with a real result. A zero-usage `usage` node with `status`
        set is enough for telemetry.py to count a 429/5xx/connect_error/
        overflow without inventing a second node type; `cost_usd=None`
        (never 0.0) so a stats sum never mistakes "no data" for "free"."""
        self.log.append_usage(
            {}, None, model=self.model_ref.raw,
            route=self._ROUTE_LABELS.get(self.model_ref.provider, self.model_ref.provider),
            status=status, retries=retries,
        )

    def _account_usage(self, result: "_StepResult") -> None:
        """Record cost/usage/OTPM bookkeeping for ONE model call's real
        `result.usage` -- called for EVERY `_step` call `_turn_body` makes,
        including one whose assistant content is later discarded (H2 scope
        B's tool_choice=required retry: a discarded attempt still spent
        real tokens against the account, so it must still count here even
        though it never becomes a logged assistant message)."""
        cost = self.cost_meter.add_usage(self.model_ref.provider, result.usage)
        # H10 Part A: telemetry.py's own source of truth -- see
        # `agent/log.py`'s `append_usage` docstring for why these extra
        # keys never touch a derived request.
        self.log.append_usage(
            result.usage, cost, model=self.model_ref.raw,
            route=self._ROUTE_LABELS.get(self.model_ref.provider, self.model_ref.provider),
            provider=self._responding_provider_label(result), finish_reason=result.finish_reason,
            latency_ms=result.latency_ms, ttft_ms=result.ttft_ms, retries=result.retries, status=result.status,
            ttfb_ms=result.ttfb_ms, first_reasoning_ms=result.first_reasoning_ms,
            first_text_ms=result.first_text_ms, first_tool_ms=result.first_tool_ms,
            reasoning_streamed=result.reasoning_streamed,
        )
        if self.model_ref.provider == "databricks":
            record_databricks_output_tokens(self.model_ref.raw, generated_tokens_for_otpm(result.usage))
        # H5 scope B: the provider's own reported prompt size drives the
        # compaction trigger (`_maybe_auto_compact`) -- "the provider's last
        # prompt_tokens (else estimate)" per the brief. H5b finding 4: on a
        # cached native Claude route (`ant:`, Databricks Claude passthrough)
        # `message_start`'s own `input_tokens` reports ONLY the uncached
        # portion -- verified: a 200k-context session with 150,000 tokens
        # sitting in cache reported `input_tokens=40`, so the trigger
        # (140,000) never fired and auto-compaction silently never ran
        # until a hard overflow. The prompt's REAL size is
        # input + cache_read + cache_creation, everywhere this number is
        # used for anything size-related (the trigger here; context %/
        # status-bar tokens and cost, both already usage-driven elsewhere).
        total_prompt_tokens = _total_prompt_tokens(result.usage)
        if total_prompt_tokens is not None:
            self._last_prompt_tokens = total_prompt_tokens

    def _maybe_yield_improve_hint(self) -> Iterator[events.Event]:
        """H10 Part B4: counters only, no model call, fires AT MOST once
        per session -- a status-bar hint (`events.status`'s own
        `improve_hint` field) plus one `notification(level="info")` event.
        Never a card, never blocks; identical in auto mode (a hint,
        nothing more) -- called from the SAME place `_account_usage`
        already runs from, so this can never fire mid-tool-dispatch or
        interrupt anything already in flight."""
        if self._improve_hint_fired:
            return
        from halo_harness.improve.config import load_improve_config
        from halo_harness.improve.hint import should_hint
        try:
            cfg = load_improve_config()
            count = should_hint(self.log.nodes(), cfg)
        except Exception:
            return
        if count is None:
            return
        self._improve_hint_fired = True
        # Text only (never `events.status`, whose `phase` field the status
        # bar switches on -- inventing a new phase value there risks
        # breaking its rendering); the TUI's own notification handler
        # (tui/dispatch.py) is what actually surfaces this in the status
        # bar / as a toast, matching every other `notification` event.
        yield events.notification(f"✦ /improve: {count} candidate cluster(s) ready to review "
                                   f"(/improve to review)", level="info")

    # ---- compaction (H5 scope B) -----------------------------------------

    def _build_body_for_messages(self, system_text: str, messages: list, tools, *,
                                  tool_choice=None, requested_max_tokens: Optional[int] = None,
                                  no_thinking: bool = False) -> dict:
        """Build a wire body for an ARBITRARY (system_text, messages, tools)
        triple using this session's own route/profile/effort -- shared by
        `_derive_and_build` (derives straight from the log) and
        `_run_compaction` (derives from the log too, then appends one extra
        instruction message before building, so it cannot reuse
        `_derive_and_build` itself). `no_thinking` (finding 15, h4-h5-h3c
        review): the summariser call never requests thinking, regardless
        of `self.effort` -- its `max_tokens` (4,096) is too small for even
        the minimum viable thinking budget, and OpenCode's own rule is a
        tool/thinking-less summarisation call outright; the pre-fix
        version passed `self.effort` through unchanged, 400ing on Anthropic
        routes for every compaction whenever any `--effort` was set."""
        if requested_max_tokens is None:
            requested_max_tokens = max_tokens_budget(self.provider_profile, model_key=self.model_ref.raw,
                                                       requested=None, abort=self.abort)
        effort = None if no_thinking else self.effort
        if self.route.dialect == "anthropic-passthrough":
            return build_anthropic_request_body(
                system_text=system_text, messages=messages, tools=tools, route=self.route,
                profile=self.provider_profile, effort=effort,
                requested_max_tokens=requested_max_tokens, tool_choice=tool_choice,
            )
        return build_request_body(
            system_text=system_text, messages=messages, tools=tools, route=self.route,
            profile=self.provider_profile, effort=effort,
            context_tokens=self.model_profile.context_tokens,
            prompt_estimate=_rough_estimate(system_text, messages),
            requested_max_tokens=requested_max_tokens, tool_choice=tool_choice,
        )

    def _count_tokens_via_api(self) -> Optional[int]:
        """H8 must-do: wire providers/http.py's `call_databricks_count_tokens`
        relay (built, never called before this) into the compaction gate,
        on any route that actually has a real count-tokens endpoint --
        native Anthropic (`ant:`) or a Databricks Claude passthrough (the
        same `/v1/messages/count_tokens` shape either host accepts). Never
        raises and never the ONLY way to estimate prompt size: None on any
        failure at all (wrong dialect, no creds, network error, a non-200,
        an unparseable body) -- the caller always falls back to
        `_rough_estimate`."""
        if self.route.dialect != "anthropic-passthrough" or self.creds is None:
            return None
        try:
            from halo_harness.providers.http import call_databricks_count_tokens
            system_text, messages, tools = derive_request(self.log, tools=None)
            body = build_anthropic_request_body(
                system_text=system_text, messages=messages, tools=tools, route=self.route,
                profile=self.provider_profile, requested_max_tokens=1,
            )
            body.pop("stream", None)
            body.pop("max_tokens", None)
            result = call_databricks_count_tokens(
                self.creds.base_url, self.creds.api_key, body, self.extra_headers or {}, self.state_dir,
            )
            try:
                if result.status != 200 or result.resp is None:
                    return None
                raw = result.resp.read()
                parsed = json.loads(raw.decode("utf-8", "replace"))
                count = parsed.get("input_tokens")
                return count if isinstance(count, int) else None
            finally:
                # NEW (H9 post-acceptance): this is a one-shot call (read
                # the whole response, done) -- `result.conn` was never
                # closed on ANY path out of this function (success, a
                # non-200 status, or an unparseable body), leaking one
                # socket per compaction-gate check until GC (a real
                # ResourceWarning, verified).
                try:
                    if result.conn is not None:
                        result.conn.close()
                except Exception:
                    pass
        except Exception:
            return None

    def _maybe_auto_compact(self, turn_no: int) -> Iterator[events.Event]:
        """Called right after `_account_usage` on every successful step:
        the 80%-of-headroom gate (agent/compact.py), using the provider's
        OWN last-reported `input_tokens` when available (the common case --
        every step after the first reply of the session, or after a
        compaction, sets it); otherwise a REAL count from the route's own
        count-tokens endpoint when it has one (H8 must-do), else a rough
        len/4 estimate of the log as it stands right now."""
        if self._last_prompt_tokens is not None:
            prompt_tokens = self._last_prompt_tokens
        else:
            counted = self._count_tokens_via_api()
            if counted is not None:
                prompt_tokens = counted
            else:
                system_text, messages, _ = derive_request(self.log, tools=None)
                prompt_tokens = _rough_estimate(system_text, messages)
        do_it, trigger_tokens = should_compact(
            prompt_tokens, self.model_profile.context_tokens, self.model_profile.max_output_tokens,
            self._compaction_knobs,
        )
        # finding 1: never auto-compact twice in a row with no successful
        # real model step between -- `_just_compacted` is reset
        # unconditionally right after this ONE check so the NEXT step's
        # usage update starts a fresh decision (a compaction that DID free
        # enough room naturally clears the trigger anyway; this guard only
        # bites when it didn't).
        if do_it and self._just_compacted:
            log.warning(
                "skipping back-to-back auto-compaction: prompt still ~%d tokens (trigger ~%d) "
                "immediately after the previous auto-compaction -- it did not free enough room",
                prompt_tokens, trigger_tokens,
            )
            # H5b finding 3: this is a deliberate SKIP (compaction was never
            # attempted), not a failure -- `phase="failed"` here used to
            # make the TUI show "✗ Compaction failed" for something that
            # never actually ran and never touched the log.
            yield events.compaction(phase="skipped", trigger="auto", turn=turn_no,
                                     reason="still over the trigger right after the previous compaction")
            do_it = False
        self._just_compacted = False
        if not do_it:
            return
        # H5c finding 22: never retry immediately after a FAILED
        # auto-compaction attempt (a persistent 429, or a summariser
        # failure) -- back off for a few steps instead of repeating the
        # whole summarisation call (plus its own retry ladder/waits) on
        # every single step. Also armed below when a compaction RAN but
        # (finding 3) left the log's estimate still at/above the trigger --
        # both cases mean "that attempt bought us nothing, don't repeat it
        # right away". Ticks down by one every step it suppresses.
        if self._compaction_backoff_remaining > 0:
            self._compaction_backoff_remaining -= 1
            yield events.compaction(phase="skipped", trigger="auto", turn=turn_no,
                                     reason=(f"backing off after a failed/ineffective auto-compaction attempt "
                                             f"({self._compaction_backoff_remaining} more step(s) before retrying)"))
            return
        compacted = yield from self._run_compaction(turn_no, trigger="auto")
        self._just_compacted = bool(compacted)
        if not compacted:
            self._compaction_backoff_remaining = _COMPACTION_FAILURE_BACKOFF_STEPS
            return
        # H5b/H5c finding 3: a compaction that RAN but left the log's own
        # estimate STILL at or above the SAME trigger achieved nothing
        # useful for this decision -- the unavoidable retained tail (plus
        # system prompt/re-injected snapshots) alone already exceeds a
        # trigger this tight, so an immediate retry next step would just
        # repeat the identical doomed summarisation call. Treated the same
        # as an outright failure for throttling purposes (back off, rather
        # than the OLD one-shot-only guard's every-OTHER-step retry cadence
        # -- verified: a 32,768/29,491 profile made 3 summariser calls, at
        # steps 1, 3 and 5, for one 5-step Read turn).
        after_system, after_messages, _ = derive_request(self.log, tools=None)
        tokens_after = _rough_estimate(after_system, after_messages)
        if tokens_after >= trigger_tokens:
            log.warning(
                "auto-compaction ran but the log is still ~%d tokens (trigger ~%d) -- the retained "
                "tail may be structurally too big for this window; backing off %d step(s)",
                tokens_after, trigger_tokens, _COMPACTION_FAILURE_BACKOFF_STEPS,
            )
            self._compaction_backoff_remaining = _COMPACTION_FAILURE_BACKOFF_STEPS

    def _compaction_model_override(self):
        """H8: `compactionModel` (settings.json/config.json, resolved by
        `agent/compact.resolve_knobs`) actually used by the summariser --
        it used to be resolved and then never read anywhere. Returns a
        `(saved_state_or_None)` token for `_restore_compaction_model`;
        temporarily swaps `self.route`/`self.provider_profile`/
        `self.model_profile`/`self.model_ref` (everything `_build_request`/
        `_build_body_for_messages` read off `self`) to the resolved
        override for the DURATION of the summarisation call(s) only.
        Same-provider only (falls back to the main model otherwise) --
        `self.creds` is a single set for the whole Session, same documented
        limitation as `_call_model_for_hook`'s own `small_model_ref` (true
        cross-provider routing needs separate credential resolution, a
        follow-up refinement, not this one-liner)."""
        raw = self._compaction_knobs.compaction_model
        if not raw or raw == self.model_ref.raw:
            return None
        try:
            routes = self.agent_runtime.routes or {}
            ref = parse_model_ref(raw, routes)
        except InvalidModelError:
            log.warning("compactionModel %r did not resolve to a valid model ref; using the session's main model", raw)
            return None
        if ref.provider != self.model_ref.provider:
            log.warning("compactionModel %r is on a different provider (%s) than the session's own creds (%s); "
                        "using the session's main model for this summary", raw, ref.provider, self.model_ref.provider)
            return None
        route = Route(provider=ref.provider, upstream_model=ref.model, dialect=ref.dialect)
        profile = resolve_profile(route)
        model_profile = resolve_model_profile(ref, self.state_dir, routes)
        saved = (self.route, self.provider_profile, self.model_profile, self.model_ref)
        self.route, self.provider_profile, self.model_profile, self.model_ref = route, profile, model_profile, ref
        return saved

    def _restore_compaction_model(self, saved) -> None:
        if saved is not None:
            self.route, self.provider_profile, self.model_profile, self.model_ref = saved

    def _run_summary_call(self, system_text: str, call_messages: list, tools, requested_max_tokens: int):
        """One streamed summarisation-call attempt, reusing `_step`'s OWN
        transport retry ladder (finding 4: the old code funnelled a
        retryable 429/5xx through the CONTENT-validation corrective retry
        instead, which resends immediately with no backoff and treats a
        transient failure exactly like a badly-shaped reply). Returns
        `(text, stop_reason, outcome)` where `outcome` is one of:
          * "ok" -- a clean reply; `text`/`stop_reason` are real.
          * "overflow" -- phase 1 raised ContextOverflow on THIS shape (the
            caller falls back to a smaller/flatter shape, or gives up).
          * "aborted" -- Esc/Ctrl+C fired mid-call; never retried.
          * "failed" -- a non-retryable (or retry-exhausted) upstream
            failure; never retried further by this method.
        Never raises -- every exception path this generator's callers care
        about is translated into one of the outcomes above."""
        attempts = 0
        while True:
            attempts += 1
            body = self._build_body_for_messages(system_text, call_messages, tools,
                                                  requested_max_tokens=requested_max_tokens, no_thinking=True)
            req = self._build_request(body)
            gen = self._stream(req)
            text_parts: list = []
            stop_reason = None
            wire_error: Optional[dict] = None
            phase1_failure: Optional[Exception] = None
            try:
                for ev in gen:
                    if self.abort.is_set():
                        break
                    kind = ev.get("type")
                    if kind == "content_block_delta":
                        delta = ev.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text_parts.append(str(delta.get("text", "")))
                    elif kind == "message_delta":
                        delta = ev.get("delta") or {}
                        if delta.get("stop_reason") is not None:
                            stop_reason = delta.get("stop_reason")
                        if isinstance(ev.get("usage"), dict):
                            cost = self.cost_meter.add_usage(self.model_ref.provider, ev["usage"])
                            # H10 Part A: a compaction summariser call is a
                            # real model call against the same account --
                            # `model`/`route`/`finish_reason` cost nothing to
                            # attach here; `provider`/ttft/latency stay
                            # unset (this loop never captured per-chunk
                            # OpenRouter provider info or a call start time,
                            # unlike `_step`'s own richer instrumentation).
                            self.log.append_usage(
                                ev["usage"], cost, model=self.model_ref.raw,
                                route=self._ROUTE_LABELS.get(self.model_ref.provider, self.model_ref.provider),
                                finish_reason=stop_reason, status="ok",
                            )
                    elif kind == "error":
                        wire_error = ev.get("error") or {}
                        break
            except (ContextOverflow, ProviderNotConfigured, UpstreamError) as e:
                phase1_failure = e
            finally:
                gen.close()

            if self.abort.is_set():
                return "".join(text_parts), "aborted", "aborted"

            if phase1_failure is not None:
                if isinstance(phase1_failure, ContextOverflow):
                    return "", None, "overflow"
                if isinstance(phase1_failure, ProviderNotConfigured):
                    return "", None, "failed"
                e = phase1_failure  # UpstreamError
                # 1.0.1 hotfix 2: same connect-failure short-circuit as
                # `_step` above -- never ladder-retry a DNS/refused/
                # unreachable failure just because a compaction summarisation
                # call happened to hit it.
                if is_connect_failure_message(e.message):
                    return "", None, "failed"
                merged_retryable = e.retryable or is_retryable_message(
                    e.status, e.message, host=self.model_ref.provider,
                )
                if merged_retryable and attempts <= MAX_RETRIES:
                    hdrs = {"retry-after": e.retry_after} if e.retry_after else {}
                    delay = _capped_retry_delay(attempts, hdrs)
                    if self._abort_sleep(delay):
                        return "", "aborted", "aborted"
                    continue
                return "", None, "failed"

            if wire_error is not None:
                message = wire_error.get("message", "unknown upstream error")
                retryable = wire_error.get("type") in ("overloaded_error", "rate_limit_error", "api_error")
                merged_retryable = retryable or is_retryable_message(
                    wire_error.get("status"), message, host=self.model_ref.provider,
                )
                if merged_retryable and attempts <= MAX_RETRIES:
                    hdrs = {"retry-after": wire_error.get("retry_after")} if wire_error.get("retry_after") else {}
                    delay = _capped_retry_delay(attempts, hdrs)
                    if self._abort_sleep(delay):
                        return "", "aborted", "aborted"
                    continue
                return "", None, "failed"

            return "".join(text_parts), stop_reason, "ok"

    def _run_compaction(self, turn_no: int, *, trigger: str, custom_instructions: Optional[str] = None) -> Iterator[events.Event]:
        """The dsh replay: one summarisation call whose prefix is the EXACT
        current derived request (pruned first -- finding 4), plus one final
        user instruction demanding the 8-section checkpoint; validated with
        one corrective retry; new transcript = `<compacted-summary>`
        (merging any prior one, via the replay prefix -- see agent/
        derive.py) + a verbatim tail + re-injected snapshots (CLAUDE.md,
        memory, environment, deferred-tool names, the active plan file --
        finding 18) + a files-read list, logged behind a `compacted` marker
        so `derive_request` skips everything before it.

        finding 4: NEVER writes the `compacted` marker on a failure with no
        usable summary text (a hard ContextOverflow on the summarisation
        call itself even after the OpenCode-style flattened-serialisation
        fallback, an exhausted-retries/non-retryable upstream failure, or
        Esc) -- returns False (via the generator's return value) instead,
        leaving the session log byte-for-byte unchanged, and yields
        `phase="failed"` with a human-readable `reason` so a caller/UI can
        react. Returns True iff the marker + replacement content were
        actually written.

        H11b finding 22: a `cc:` session's model-visible context lives in
        Claude Code itself (module docstring, agent/cc_runtime.py) --
        `derive_request` is never what a `cc:` turn actually sends, so
        summarising ITS output and splicing a `compacted` marker into
        THIS log would compact something the model never even sees.
        Claude Code auto-compacts its own context; this is the documented
        no-op (README: "`/compact` prints a note")."""
        if self.model_ref.provider == "cc":
            yield events.compaction(
                phase="failed", trigger=trigger, turn=turn_no,
                reason="halo's own compaction is a no-op for cc: sessions -- Claude Code "
                       "manages its own context/compaction internally.",
            )
            return False
        system_text, raw_messages, tools = derive_request(self.log, tools=None)
        # finding 4: prune_messages used to be wired ONLY into `/context` --
        # applied here too, so the summarisation call's own prefix benefits
        # from the same protection-window shrink an ordinary step now gets
        # (`_derive_and_build`), making the exact-prefix attempt below less
        # likely to need the flattened fallback at all. H5b finding 1:
        # ONLY for the summarisation call's own request body -- `raw_
        # messages` (unpruned) is what `select_verbatim_tail` below must
        # select from, or a stubbed/truncated copy gets baked permanently
        # into the freshly-compacted log.
        messages = self._pruned_messages_for_wire(raw_messages)
        tokens_before = self._last_prompt_tokens or _rough_estimate(system_text, messages)
        yield events.compaction(phase="start", trigger=trigger, turn=turn_no, tokens_before=tokens_before)

        if self.hook_runner is not None and self.hook_runner.has_hooks("PreCompact"):
            payload = self.hook_runner.payload("PreCompact", extra={"trigger": trigger,
                                                                      "custom_instructions": custom_instructions})
            # H5c finding 12: `abort` threaded through -- Esc during a slow
            # PreCompact command hook used to be completely ignored.
            outcome = self._run_hook("PreCompact", payload, matched=trigger, abort=self.abort)
            if outcome.additional_context:
                self.log.append_snapshot([{"type": "text", "text": outcome.additional_context}], kind="hook_context")

        saved_model = self._compaction_model_override()
        try:
            instruction = build_summary_instruction(custom_instructions)
            summary_text, stop_reason, ok, missing = "", None, False, []
            call_messages_base, call_tools = messages, tools
            tried_fallback = False
            fatal_reason: Optional[str] = None
            content_retries = 0
            max_content_retries = 1  # "one corrective retry" (unchanged from before finding 4)
            while True:
                call_messages = call_messages_base + [{"role": "user", "content": [{"type": "text", "text": instruction}]}]
                text, stop_reason, outcome = self._run_summary_call(
                    system_text, call_messages, call_tools,
                    min(4096, self.model_profile.max_output_tokens or 4096),
                )
                if outcome == "overflow":
                    if not tried_fallback:
                        # finding 4: OpenCode-style fallback -- flatten the
                        # WHOLE (already-pruned) transcript to plain text in
                        # ONE user message, no `tools` offered, and retry
                        # once more with that far smaller/simpler shape
                        # before giving up.
                        tried_fallback = True
                        # H5c finding 22: bound the fallback body itself to a
                        # share of THIS model's own context window (chars,
                        # via the same 4-chars/token rule of thumb used
                        # everywhere else in this module) -- never unbounded,
                        # so it cannot overflow the SAME small window a
                        # second time with no further retry left.
                        fallback_cap = min(
                            _SUMMARY_FALLBACK_MAX_CHARS,
                            max(_SUMMARY_FALLBACK_MIN_CHARS,
                                int(self.model_profile.context_tokens * CHARS_PER_TOKEN * _SUMMARY_FALLBACK_HEAD_SHARE)),
                        )
                        call_messages_base = [{"role": "user", "content": [
                            {"type": "text", "text": _serialize_transcript_for_summary(messages, max_chars=fallback_cap)},
                        ]}]
                        call_tools = []
                        log.warning("compaction (%s): summarisation call overflowed on the exact prefix; "
                                    "retrying once with a flattened, tool-less serialisation", trigger)
                        continue
                    fatal_reason = ("the summarisation call overflowed even after falling back to a "
                                     "flattened, tool-less serialisation of the conversation")
                    break
                if outcome == "aborted":
                    fatal_reason = "interrupted by the user"
                    break
                if outcome == "failed":
                    fatal_reason = "the summarisation call failed (see logs)"
                    break
                # outcome == "ok": real text -- validate its shape as before.
                summary_text = text
                ok, missing = validate_summary(summary_text, stop_reason)
                if ok or content_retries >= max_content_retries:
                    break
                content_retries += 1
                yield events.compaction(phase="retry", trigger=trigger, turn=turn_no, headings_missing=missing)
                instruction = build_summary_instruction(
                    custom_instructions, corrective=True,
                    problem=(f"missing sections: {', '.join(missing)}" if missing else "the reply was cut off"),
                )

            if fatal_reason is not None or not summary_text.strip():
                reason = fatal_reason or "the model returned no usable summary text"
                log.warning("compaction (%s) failed: %s -- leaving the session log unchanged", trigger, reason)
                yield events.compaction(phase="failed", trigger=trigger, turn=turn_no,
                                         tokens_before=tokens_before, reason=reason)
                return False
            if not ok:
                log.warning("compaction summary failed validation twice (missing=%s); using it anyway", missing)
        finally:
            self._restore_compaction_model(saved_model)

        wrapped = wrap_compacted_summary(summary_text)
        usable = _opencode_usable(self.model_profile.context_tokens, self.model_profile.max_output_tokens)
        # H5b finding 1: select the tail from the UNPRUNED transcript, never
        # the copy stubbed/head-tailed for the summarisation call's own
        # request above -- otherwise a stubbed tool_result got written
        # permanently into the freshly-compacted log.
        tail = select_verbatim_tail(raw_messages, tail_retention_tokens(usable))

        self.log.append_compacted(trigger=trigger, custom_instructions=custom_instructions)
        self.log.append_user([{"type": "text", "text": wrapped}], kind="compaction_summary")

        # finding 18: re-inject EVERY session-start snapshot kind, not just
        # CLAUDE.md/memory/files-read -- environment and the deferred-tool-
        # names reminder used to be logged ONCE, at session start, and
        # never again, so they silently vanished from the model's view
        # after the FIRST compaction (derive_request skips everything
        # before the `compacted` marker just written above). The active
        # plan file (if this session is in plan mode) is re-attached the
        # same way EnterPlanMode originally logged it.
        ctx_obj = self.session_context
        claude_md = ctx_obj.claude_md_text() if ctx_obj is not None else ""
        if claude_md:
            self.log.append_snapshot([{"type": "text", "text": claude_md}], kind="claude_md")
            self._fire_instructions_loaded(claude_md)
        memory_text = ctx_obj.memory_snapshot_text() if ctx_obj is not None else ""
        if memory_text:
            self.log.append_snapshot([{"type": "text", "text": memory_text}], kind="memory_index")
        env_text = ctx_obj.environment_snapshot_text(self.model_label) if ctx_obj is not None else ""
        if env_text:
            self.log.append_snapshot([{"type": "text", "text": env_text}], kind="environment")
        if self.session_catalog is not None and self.session_catalog.deferred:
            deferred_names = sorted(self.session_catalog.deferred)
            reminder = (
                "The following deferred tools are now available via ToolSearch. Their schemas "
                "are NOT loaded -- calling them directly will fail with InputValidationError. Use "
                "ToolSearch with query \"select:<name>[,<name>...]\" to load tool schemas before "
                "calling them:\n" + "\n".join(deferred_names)
            )
            self.log.append_snapshot([{"type": "text", "text": reminder}], kind="deferred_tools")
        if self.permission_engine.mode == "plan" and self.permission_engine.plan_file is not None:
            plan_note = PLAN_MODE_NOTE
            try:
                plan_text = Path(self.permission_engine.plan_file).read_text(encoding="utf-8")
            except OSError:
                plan_text = ""
            if plan_text.strip():
                plan_note += f"\n\nCurrent plan file ({self.permission_engine.plan_file}):\n\n{plan_text}"
            self.log.append_snapshot([{"type": "text", "text": plan_note}], kind="plan_mode")
        files_text = build_files_read_snapshot(list(self._read_cache.keys()))
        if files_text:
            self.log.append_snapshot([{"type": "text", "text": files_text}], kind="files_read")

        for msg in tail:
            if msg.get("role") == "user":
                self.log.append_user(msg.get("content") or [], kind="compaction_tail")
            elif msg.get("role") == "assistant":
                # H5c finding 16: a thinking block's signature is bound to
                # the EXACT prefix that preceded it when the model produced
                # it (Anthropic's "preserved thinking" check) -- the tail
                # is about to be re-appended right after a brand-new
                # `<compacted-summary>` node, a COMPLETELY different
                # prefix, so any signature here is now stale and would
                # fail that check on the very next request (verified
                # against the Claude API docs; on Fable 5.1/Opus 5.5, a
                # replayed signature that doesn't match invalidates the
                # request outright). Stripped here, once, rather than
                # replayed and rejected forever after every future
                # compaction of an already-compacted session.
                stripped = [b for b in (msg.get("content") or [])
                            if not (isinstance(b, dict) and b.get("type") == "thinking")]
                if any(_assistant_block_is_replayable(b) for b in stripped):
                    self.log.append_assistant(content=stripped, reasoning=msg.get("reasoning"))

        if self.hook_runner is not None and self.hook_runner.has_hooks("PostCompact"):
            payload = self.hook_runner.payload("PostCompact", extra={"trigger": trigger, "compact_summary": summary_text})
            self._run_hook("PostCompact", payload, matched=trigger)
        self._fire_session_start("compact")

        after_system, after_messages, _ = derive_request(self.log, tools=None)
        tokens_after = _rough_estimate(after_system, after_messages)
        self._last_prompt_tokens = None  # unknown until the next real reply's usage lands
        # H5b finding 2: every committed/pending stub id refers to a
        # tool_result that this compaction just replaced with the summary +
        # a fresh verbatim tail -- start pruning's batch fresh rather than
        # carrying stale ids (and a stale pending-token count) forward.
        self._reset_prune_state()
        yield events.compaction(phase="done", trigger=trigger, turn=turn_no,
                                 tokens_before=tokens_before, tokens_after=tokens_after)
        return True

    # ---- the turn: model call(s) + tool dispatch ------------------------

    def turn(self, text: str, images: Optional[list] = None) -> Iterator[events.Event]:
        """2.0.1 W3a/W3b: a thin wrapper around `_turn_inner` (this method's
        own FULL body, unchanged, just renamed) -- every event either of
        them would ever yield passes through `self._timeline.record_turn_
        event` here FIRST, the ONE choke point that sees every turn's
        events regardless of dialect (native/openai-chat/cc-subprocess) or
        caller (the TUI's drain loop, headless -p's sink) without needing
        its own instrumentation inside `_turn_body`/`cc_runtime.turn_body_
        cc`. The finished record is appended to the session log as one
        `timeline` node (`halo bugreport`/`/timeline`/`halo timeline --last
        N` all read it back from there -- see `agent/log.py`'s own node
        kinds). `self._timeline` (W3b item 11): THIS session's own
        instance, never the `debug_timeline` module's shared default --
        see `Session.__init__`'s own comment."""
        self._timeline.start_turn(self.turn_count + 1)
        try:
            for ev in self._turn_inner(text, images=images):
                self._timeline.record_turn_event(ev)
                yield ev
        finally:
            record = self._timeline.end_turn()
            if record is not None:
                try:
                    # `append_meta` (never `append_snapshot`) -- a `meta`
                    # node carries no transcript CONTENT `derive_request`
                    # ever replays to the model (agent/derive.py's own
                    # `kind == "meta"` branch only ever reads `tools` off
                    # one); a snapshot's own `content` list, by contrast,
                    # becomes a real user-role message the model would see
                    # raw internal timing/tool-status telemetry inside.
                    self.log.append_meta(timeline=record)
                except Exception:
                    pass

    def _turn_inner(self, text: str, images: Optional[list] = None) -> Iterator[events.Event]:
        """Run one turn to completion (which may involve several model
        calls interleaved with tool dispatch, bounded by `--max-turns`
        MODEL CALLS -- finding 9, see `_turn_body`). Always ends by
        yielding a `turn_done` event."""
        self.turn_count += 1
        turn_no = self.turn_count
        # W4a `--fallback-model`: "re-tries the primary at the start of each
        # user turn" -- reset here so a fallback used (if ever) by a PRIOR
        # turn never sticks around; `apply_next_fallback_model` consumes
        # this list from the front as retries are exhausted within a turn.
        # (The swap itself is not yet wired into the model-call retry loop
        # -- see the worker report -- so this seeds the list correctly but
        # a fallback is not automatically applied yet.)
        self._fallback_remaining = list(self.fallback_models)
        self._check_config_change()
        self._loop_breaker = {}
        self._loop_breaker_history = []
        self._loop_breaker_period2 = {}
        # H3/H4 must-do: a PREVIOUS turn's interrupt (Esc/Ctrl+C) leaves
        # `self.abort` set -- reused unchanged, a brand new turn would see
        # it already fired and abort immediately, before ever streaming a
        # single token. Each turn starts with a clean slate.
        # H5c finding 7: but ONLY when this Session OWNS the Event --
        # a sub-agent shares the PARENT's own abort Event (see `__init__`),
        # and clearing a SHARED Event here would silently undo an Esc the
        # user fired for the whole session, from whichever child (or a
        # queued one that starts after the pool frees a worker) happens to
        # begin its own turn() next.
        if self._owns_abort:
            self.abort.clear()
        self._stop_hook_active = False
        # H4 scope C: a Skill's `allowed-tools` only ever lasts "until the
        # next user message" -- this IS that next user message.
        self.permission_engine.clear_temporary_allow_rules()
        # scope 0(c): `busy` (and therefore whether Controller.submit
        # routes new input to a fresh turn or to `steer`) is True for the
        # WHOLE method body below, cleared no matter how it ends.
        self._busy.set()
        # H11b finding 4: captured here (this hook block's own scope ends
        # before the cc: dispatch below) so a UserPromptSubmit hook's
        # additionalContext -- already logged as a snapshot two lines down
        # from where it's set -- also reaches a `cc:` turn's own stdin
        # line; every other route already sees it via `derive_request`
        # picking the snapshot up like any other logged node.
        hook_context_text: Optional[str] = None
        try:
            if self.hook_runner is not None and self.hook_runner.has_hooks("UserPromptSubmit"):
                payload = self.hook_runner.payload("UserPromptSubmit", prompt_id=f"prompt_{turn_no}",
                                                     extra={"prompt": text})
                # H5c finding 12: `abort` threaded through -- Esc during a
                # slow UserPromptSubmit command hook used to be completely
                # ignored (the WHOLE turn couldn't even start until it
                # returned).
                outcome = self._run_hook("UserPromptSubmit", payload, matched="", abort=self.abort)
                for msg in outcome.system_messages:
                    yield events.notification(msg)
                if outcome.blocked:
                    # D-CFG/B: "blocked -> prompt dropped + reason shown" --
                    # the prompt is NEVER appended to the log at all.
                    reason = outcome.block_reason or "blocked by a UserPromptSubmit hook"
                    yield events.notification(f"Prompt dropped: {reason}", level="error")
                    yield events.status(phase="idle", model=self.model_ref.raw, turn=turn_no,
                                         cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None)
                    yield events.turn_done(turn=turn_no, reason="blocked")
                    return
                if outcome.additional_context:
                    self.log.append_snapshot([{"type": "text", "text": outcome.additional_context}], kind="hook_context")
                    hook_context_text = outcome.additional_context

            blocks = [{"type": "text", "text": text}]
            for img in (images or []):
                blocks.append(img)
            self.log.append_user(blocks)
            yield events.user_message(text, turn=turn_no, images=images)
            yield events.status(
                phase="thinking", model=self.model_ref.raw, turn=turn_no,
                context_limit=self.model_profile.context_tokens,
                cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None,
            )

            try:
                if self.model_ref.provider == "cc":
                    # H11 Part B: the installed `claude` binary IS the
                    # model here -- a completely separate turn-execution
                    # path (agent/cc_runtime.py) that never touches
                    # derive_request/stream_completion; it logs its own
                    # user/assistant/tool_result/usage nodes so every OTHER
                    # route's history reconstruction, /stats, /export and
                    # resume all keep working unchanged.
                    from halo_harness.agent import cc_runtime
                    yield from cc_runtime.turn_body_cc(self, turn_no, text, images=images,
                                                        hook_context=hook_context_text)
                else:
                    yield from self._turn_body(turn_no)
            except GeneratorExit:
                # rule 2/scope F: an interrupted turn must never leave a
                # tool_use without a matching tool_result in the log.
                synthesize_missing_results(self.log, reason="Tool call interrupted by user")
                raise
            except BaseException:
                synthesize_missing_results(self.log, reason="ABORTED_BEFORE_DISPATCH")
                raise
        finally:
            # finding 5 (major, h4-h5-h3c review): a steer that lands AFTER
            # the turn's last internal checkpoint -- during Stop hooks, the
            # final status/turn_done yields, or an error/Esc unwind -- was
            # accepted by `steer()` (busy was still True) but then never
            # applied by anything, and silently vanished once `_busy.clear()`
            # ran. Drained here and `_busy.clear()` under the SAME
            # `_steer_lock`, atomically with `steer()`'s own busy-check +
            # enqueue (see its docstring) so there is no window where a
            # steer can be accepted after this drain already ran. `run()`
            # resubmits whatever's left as the NEXT `user_input`, right
            # after this call returns.
            #
            # H9 whole-tree review finding 17: `_end_busy_period` ALSO
            # drains `_pending_worker_writes` now -- see its own docstring
            # for the bug this closes (a write queued too late for any of
            # `_apply_pending_steers_events`'s own safe points inside
            # `_turn_body` used to sit stranded here until the NEXT turn's
            # first safe point, applied only after that next turn's own
            # first model reply had already been derived without it).
            self._end_busy_period()

    def _end_busy_period(self) -> None:
        """Shared by `turn()`'s own `finally` and `_pump_compaction`/
        `_pump_clear` below (H9 whole-tree review finding 17: a manual
        `/compact`/`/clear` never set `_busy` at all, so `queue_log_write`
        -- called from the UI thread for a live `@mention`/`!cmd` typed
        during either -- saw `_busy.is_set()` False and wrote STRAIGHT INTO
        `self.log`, racing the worker thread's own concurrent compaction/
        clear writes; verified: an `@notes.txt` snapshot written during a
        manual `/compact` landed in the log BEFORE the `compacted` marker,
        staying invisible to the model for that exact compaction call).
        Atomically: leftover steers stashed for `run()` to resubmit,
        `_pending_worker_writes` popped, `_busy` cleared -- all under
        `_steer_lock`, matching `steer()`'s own busy-check+enqueue so
        nothing can be accepted into either queue after this point without
        this method seeing it first. The popped writes are then applied
        OUTSIDE the lock (log writes are independently thread-safe --
        `SessionLog._append` has its own lock -- so there's no reason to
        hold `_steer_lock` any longer than the pop itself needs)."""
        with self._steer_lock:
            self._leftover_steer_texts.extend(self._steer_queue)
            self._steer_queue = []
            pending_writes, self._pending_worker_writes = self._pending_worker_writes, []
            self._busy.clear()
        for kind, payload in pending_writes:
            self._apply_log_write(kind, payload)

    def _turn_body(self, turn_no: int) -> Iterator[events.Event]:
        # finding 9: `--max-turns` counts MODEL CALLS made WITHIN this one
        # turn (Claude Code semantics) -- `-p` calls `turn()` exactly once,
        # so counting turn() calls against it (the pre-H2 behavior) never
        # bounded anything inside a tool loop; only the identical-call
        # breaker (a SEPARATE guard, kept as-is) did.
        model_calls = 0
        yield from self._apply_pending_agent_notices(turn_no)
        yield from self._apply_pending_job_notices(turn_no)
        while True:
            if model_calls >= self.max_turns:
                # H5 scope F item 9 (OpenCode Appendix H): inject
                # MAX_STEPS_PROMPT as a snapshot and give the model ONE more
                # (tool-less) call to close out with a real summary instead
                # of just silently ending mid-tool-loop.
                self.log.append_snapshot([{"type": "text", "text": MAX_STEPS_PROMPT}], kind="max_steps")
                # finding 18: keep the full frozen tool catalog in `tools`
                # (an Anthropic route 400s on "tool_use ... must define
                # tools" if the history already has tool_use blocks but this
                # request's own `tools` field is empty/absent) and instead
                # forbid a NEW call via `tool_choice: "none"` -- backs
                # "Tools are disabled until next user input" with a real
                # wire constraint on every dialect, not just prompt text.
                final_result = yield from self._step(turn_no, tool_choice="none", no_tools=False)
                if final_result is not None and final_result is not _OVERFLOW_NEEDS_COMPACTION:
                    self._account_usage(final_result)
                    # H5c finding 20: `tool_choice: "none"` is a REQUEST,
                    # not a guarantee -- a provider that ignores or
                    # downgrades it can still return `tool_use` blocks
                    # here, and this wrap-up call never dispatches
                    # anything (the turn just ends), so a logged tool_use
                    # would stay unpaired forever: every LATER request
                    # carries it with no matching tool_result, which
                    # OpenAI-dialect routes paper over with a "(no result)"
                    # placeholder and Anthropic routes 400 on outright.
                    # Stripped rather than dispatched or synthesized a
                    # result for -- the model was told not to call tools
                    # this turn at all.
                    wrap_up_blocks = [b for b in final_result.assistant_blocks if b.get("type") != "tool_use"]
                    # H5b finding 5: never persist an empty/unreplayable
                    # assistant node (a steer can cut this wrap-up call
                    # short too) -- see `_assistant_block_is_replayable`.
                    if any(_assistant_block_is_replayable(b) for b in wrap_up_blocks):
                        self.log.append_assistant(
                            content=wrap_up_blocks, stop_reason=final_result.stop_reason,
                            request_hash=content_hash_from_oai_body(final_result.body),
                        )
                    # Mirrors the ordinary per-step message_end (see below)
                    # so a print-mode JSON result reflects the WRAP-UP
                    # call's own stop_reason/usage, not a stale earlier one.
                    prompt_tokens = _total_prompt_tokens(final_result.usage)
                    context_pct = (round(100.0 * prompt_tokens / self.model_profile.context_tokens, 1)
                                   if prompt_tokens is not None and self.model_profile.context_tokens else None)
                    yield events.message_end(
                        turn=turn_no, stop_reason=final_result.stop_reason, usage=final_result.usage,
                        cost_usd=(self.cost_meter.total_usd if self.cost_meter.has_cost_data else None),
                        context_pct=context_pct, context_tokens=prompt_tokens,
                        context_limit=self.model_profile.context_tokens,
                        total_input_tokens=self.cost_meter.total_input_tokens,
                        total_output_tokens=self.cost_meter.total_output_tokens,
                    )
                yield events.status(phase="idle", model=self.model_ref.raw, turn=turn_no,
                                     cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None)
                yield events.turn_done(turn=turn_no, reason="max_turns")
                return

            result = yield from self._step(turn_no)
            model_calls += 1
            if result is _OVERFLOW_NEEDS_COMPACTION:
                # H5 scope B: overflow -> compact -> retry ONCE.
                # finding 4: if compaction itself could not free any room
                # (it already yielded its own `phase="failed"` event and
                # wrote nothing to the log), retrying `_step` would just
                # overflow again on the IDENTICAL history -- report the
                # error and end the turn instead of a doomed retry.
                compacted = yield from self._run_compaction(turn_no, trigger="overflow")
                if not compacted:
                    yield events.error(
                        "context window overflow and compaction could not free enough room -- "
                        "try `/compact <instructions>` to focus the summary, or start a new session",
                        turn=turn_no, err_type="context_overflow", category=CONTEXT_WINDOW_EXCEEDED,
                    )
                    self._fire_stop_failure(turn_no, "context window overflow and compaction could not free enough room")
                    yield events.turn_done(turn=turn_no, reason="error")
                    return
                result = yield from self._step(turn_no, overflow_handled=True)
                model_calls += 1
            if result is None:
                if self.abort.is_set():
                    # U2: an interrupt ended this turn -- a clean stop, not
                    # an error (the UI shows it as "interrupted by user").
                    yield events.status(phase="idle", model=self.model_ref.raw, turn=turn_no,
                                         cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None)
                    yield events.turn_done(turn=turn_no, reason="interrupted")
                    return
                self._fire_stop_failure(turn_no, "the model call failed")
                yield events.turn_done(turn=turn_no, reason="error")
                return
            self._account_usage(result)
            yield from self._maybe_yield_improve_hint()
            yield from self._maybe_auto_compact(turn_no)

            # H2 scope B: the repair layer's text-embedded-call handling.
            # Only when a tool call was actually EXPECTED (tools were
            # offered) and none arrived (report's own leak_parser scoping
            # rule) -- try providers.hooks.leak_parser first (promotes a
            # real tool_use, `repaired=True` downstream); only if THAT
            # finds nothing AND the text still looks like an ATTEMPTED
            # (truncated/malformed) text-embedded call rather than an
            # ordinary final answer (`_looks_like_attempted_tool_call` --
            # never retry just because no tool call happened to be needed,
            # the overwhelmingly common "no tool_use_blocks" case), and the
            # profile says "required" is safe for this row (never DeepSeek-
            # thinking/GLM/Qwen), retry ONCE with tool_choice=required. The
            # original (prose-only) `result` is
            # discarded in favor of whichever of these actually produced a
            # usable call -- it is deliberately never logged; only a call
            # that will actually be DISPATCHED becomes the turn's real
            # assistant message (a discarded attempt still spent real
            # tokens, which `_account_usage` already recorded above).
            tool_use_blocks = [b for b in result.assistant_blocks if b.get("type") == "tool_use"]
            if not tool_use_blocks and result.body.get("tools") and result.stop_reason != "tool_use":
                text_so_far = "".join(b.get("text", "") for b in result.assistant_blocks if b.get("type") == "text")
                leaked = leak_parser(text=text_so_far, profile=self.provider_profile)
                # finding 2: leak_parser already scopes promotion to markup
                # that's essentially the whole message (never mid-prose
                # demonstration/explanation); the SECOND gate, only
                # available here (not in the provider-generic hooks
                # module), is that the resolved name must be a REAL tool
                # in this session's catalog -- a package.json's `"name":
                # "my-cli"` (verified: became an "Unknown tool" error that
                # replaced a perfectly good answer) never passes this.
                if leaked and leaked.get("name") and self.tool_registry.get(leaked["name"]) is not None:
                    synthetic_id = f"toolu_repair_{uuid.uuid4().hex[:20]}"
                    synthetic_block = {"type": "tool_use", "id": synthetic_id, "name": leaked["name"],
                                        "input": leaked.get("arguments") or {}, "_promoted_from_leak": True}
                    # finding 2: strip the raw leaked markup from the TEXT
                    # block(s) that get logged -- a future replay must see
                    # a clean tool_use, never the raw markup ALSO sitting
                    # there next to it (which would just teach the model to
                    # repeat the same leaky shape).
                    cleaned_blocks = []
                    for b in result.assistant_blocks:
                        if b.get("type") == "text":
                            new_text = _strip_promoted_leak_text(b.get("text", ""))
                            if new_text:
                                cleaned_blocks.append({**b, "text": new_text})
                        else:
                            cleaned_blocks.append(b)
                    result.assistant_blocks = cleaned_blocks + [synthetic_block]
                    tool_use_blocks = [synthetic_block]
                    log.debug("leak_parser promoted a text-embedded call to a real tool_use: %r", leaked)
                elif self.provider_profile.tool_choice_required_supported and _looks_like_attempted_tool_call(text_so_far):
                    retry_result = yield from self._step(turn_no, tool_choice="required")
                    model_calls += 1
                    if retry_result is not None:
                        self._account_usage(retry_result)
                        result = retry_result
                        tool_use_blocks = [b for b in result.assistant_blocks if b.get("type") == "tool_use"]
                    # else: the retry hard-failed (already emitted its own
                    # `error` event) -- fall through and log the ORIGINAL
                    # prose-only result rather than silently losing the turn.

            req_hash = content_hash_from_oai_body(result.body)  # finding 4: hash the body ACTUALLY SENT
            # H5b finding 5: a steer noticed before any block finished (or
            # mid-thinking, before signature_delta) leaves NOTHING
            # replayable in `result.assistant_blocks` -- logging it anyway
            # used to write `content: []` (or an unsigned thinking-only
            # block `prepare_anthropic_messages` then drops) permanently
            # into the log; Anthropic 400s on every LATER request once that
            # node is there. Log nothing for this step instead (mirrors the
            # abort path's own "nothing to log if there's no partial text"
            # rule above) -- a genuine tool_use always counts as replayable
            # so a call that already closed before the cut still gets
            # logged (and dispatched) normally.
            # H10 Part A: repair outcomes computed ONCE here (before the log
            # write, so `tool_meta` can ride along on the SAME node) and
            # reused by `_dispatch_tools` below via the `repair_outcomes=`
            # param -- it no longer recomputes them itself. Safe to compute
            # this early: `repair_assistant_turn`'s only side effect
            # (auto-loading a not-yet-loaded `mcp__` tool from
            # `self.session_catalog`) is idempotent and nothing between here
            # and the `_dispatch_tools` call below reads catalog state.
            repair_outcomes = (repair_assistant_turn(tool_use_blocks, self.tool_registry, catalog=self.session_catalog)
                                if tool_use_blocks else [])
            tool_meta = (build_tool_meta(tool_use_blocks, repair_outcomes, result.tool_call_flags)
                         if tool_use_blocks else {})
            if any(_assistant_block_is_replayable(b) for b in result.assistant_blocks):
                self.log.append_assistant(
                    content=result.assistant_blocks, reasoning=result.reasoning,
                    stop_reason=result.stop_reason, request_hash=req_hash, tool_meta=tool_meta,
                )
                self._fire_message_display(result.assistant_blocks)
            prompt_tokens = _total_prompt_tokens(result.usage)
            context_pct = None
            if prompt_tokens is not None and self.model_profile.context_tokens:
                context_pct = round(100.0 * prompt_tokens / self.model_profile.context_tokens, 1)
            # finding 15: cost_usd is the SESSION's cumulative total (not
            # just this one call's own cost) -- output.py's JSON sink takes
            # message_end's cost_usd as-is (the latest value naturally IS
            # the running total by construction), so a three-call turn
            # reports its whole cost, not a third of it.
            yield events.message_end(
                turn=turn_no, stop_reason=result.stop_reason, usage=result.usage,
                cost_usd=(self.cost_meter.total_usd if self.cost_meter.has_cost_data else None),
                context_pct=context_pct, context_tokens=prompt_tokens,
                context_limit=self.model_profile.context_tokens,
                total_input_tokens=self.cost_meter.total_input_tokens,
                total_output_tokens=self.cost_meter.total_output_tokens,
            )

            if not tool_use_blocks:
                applied = yield from self._apply_pending_steers_events(turn_no)
                if applied:
                    # scope 0(c): a steer (possibly the very one that cut
                    # this model call short mid-stream) is waiting -- apply
                    # it and get the model's reaction immediately, never
                    # run Stop hooks against what may be a truncated
                    # non-answer.
                    continue
                if self.hook_runner is not None and self.hook_runner.has_hooks("Stop"):
                    last_text = "".join(b.get("text", "") for b in result.assistant_blocks
                                         if b.get("type") == "text")
                    # `run_stop` (hooks.py) owns `stop_hook_active`/the
                    # consecutive-block CAP itself (its own counter,
                    # matching Claude Code's exact override message).
                    # H5c finding 12: `abort` threaded through -- Esc during
                    # a slow (up to the default 600s) Stop command hook
                    # used to be completely ignored; the hook process group
                    # is now killed and `run_stop` returns almost
                    # immediately once Esc fires (the `reason` computed
                    # below then correctly reports "interrupted").
                    stop_outcome = self._run_hook_stop("Stop", last_assistant_message=last_text,
                                                        prompt_id=f"turn_{self.turn_count}", abort=self.abort)
                    for msg in stop_outcome.system_messages:
                        yield events.notification(msg)
                    if stop_outcome.blocked:
                        # D-CFG: stderr/reason becomes a user-role message
                        # and the loop continues.
                        continuation = stop_outcome.block_reason or "Please continue."
                        self.log.append_user([{"type": "text", "text": continuation}], kind="continuation")
                        yield events.user_message(continuation, turn=turn_no)
                        continue
                yield events.status(phase="idle", model=self.model_ref.raw, turn=turn_no, context_tokens=prompt_tokens,
                                     context_limit=self.model_profile.context_tokens,
                                     cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None)
                # finding 3: a message's OWN max_tokens cutoff is not the
                # SESSION hitting --max-turns -- report it as what it is.
                # H5c finding 12: an Esc that cut a slow Stop hook short
                # (above) must be reported honestly too, not as a plain
                # "end_turn".
                if self.abort.is_set():
                    reason = "interrupted"
                else:
                    reason = "max_tokens" if result.stop_reason == "max_tokens" else "end_turn"
                yield events.turn_done(turn=turn_no, reason=reason)
                return

            ended = yield from self._dispatch_tools(turn_no, tool_use_blocks, result.tool_call_flags,
                                                     repair_outcomes=repair_outcomes)
            applied = yield from self._apply_pending_steers_events(turn_no)
            if applied:
                # scope 0(c): "tools allowed to finish" -- they just did;
                # a fresh steer overrides even a loop-breaker `end_turn`,
                # since the user's own redirect makes another identical
                # call in a row unlikely.
                continue
            if ended:
                yield events.status(phase="idle", model=self.model_ref.raw, turn=turn_no,
                                     cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None)
                # H5c finding 9: `_dispatch_tools` also returns True when it
                # ended because `self.abort` fired mid-dispatch (an Esc, or
                # now a PermissionRequest hook's own `interrupt: true`) --
                # report that honestly as `reason="interrupted"`, not the
                # same generic "end_turn" a loop-breaker/plan-mode call
                # dispatch uses for an ordinary stop.
                reason = "interrupted" if self.abort.is_set() else "end_turn"
                yield events.turn_done(turn=turn_no, reason=reason)
                return
            # HALO-2.0.1-liveness-tips-brief.md Part A1 / W2-plan item 6:
            # tool results were just dispatched back; the NEXT model call
            # (the top of this `while True:`, back in `_step`) hasn't been
            # sent yet -- the one `phase` state `_step` itself never emits
            # (it only ever covers ONE model call, never the gap between
            # two of them in the same turn).
            yield events.phase(state="waiting_for_model", turn=turn_no, model=self.model_ref.raw)
            # else: loop back for another _step() call with the new tool_results

    def _resolve_tool_call(self, tu: dict, outcome, tool_call_flags: dict) -> dict:
        """Everything about ONE tool_use call up to (never including)
        actual dispatch: length/malformed short-circuit -> basic shape
        validate -> repair outcome -> loop breaker -> permission decide.
        Returns a plain dict `_dispatch_tools` drives from (`ready=True`
        means "go dispatch me"; the reason this is its own function is so
        a run of consecutive read-only READY calls can be discovered and
        batched -- see `_dispatch_tools` -- without duplicating any of
        this decision logic)."""
        tool_id, raw_name = tu.get("id"), tu.get("name")
        item = {"tool_id": tool_id, "name": raw_name, "input": tu.get("input") or {}, "repaired": False, "ready": False}

        classification = classify_length_tool_call(tool_call_flags.get(tool_id) or {})
        if classification in ("length", "malformed"):
            if classification == "length":
                item["text"] = (f"Tool call {raw_name!r} was cut off at max_tokens mid-call -- split the "
                                 f"operation into smaller steps and retry.")
            else:
                json_error = (tool_call_flags.get(tool_id) or {}).get("json_error", "invalid JSON")
                item["text"] = f"Tool call {raw_name!r} arguments were not valid JSON: {json_error}"
            item["input"] = {}
            # H10 Part A: both a length-truncated and an unrecoverably
            # malformed call are "the model never produced usable args" --
            # lumped under schema_invalid rather than growing the taxonomy.
            item["error_class"] = "schema_invalid"
            return item

        basic_error = validate_tool_use(tu)
        if basic_error is not None:
            item["text"] = basic_error
            item["error_class"] = "schema_invalid"
            return item

        if not outcome.ok:
            item["text"] = outcome.error_text
            item["repaired"] = outcome.repaired
            # H10 Part A: a duplicate call is never "invalid" -- classed
            # "other" so it never inflates schema_invalid/not_found counts.
            item["error_class"] = {"unknown_tool": "other", "invalid_args": "schema_invalid"}.get(
                outcome.error_kind, "other")
            return item

        name = outcome.block.get("name")  # possibly renamed by repair
        tool_input = outcome.block.get("input") or {}
        # A block promoted from a text-embedded leak (H2 scope B) is
        # ALWAYS "repaired" for transparency -- it never arrived as a
        # native call, regardless of whether its name/schema also happened
        # to need fixing up.
        repaired = outcome.repaired or bool(tu.get("_promoted_from_leak"))
        item.update(name=name, input=tool_input, repaired=repaired)

        # H9 whole-tree review finding 8: polling a background job with
        # BashOutput(shell_id=...) -- Moonshot's OWN documented pattern for
        # "start a server, wait until ready, run the tests" -- calls it
        # with the SAME arguments every time by design (the shell_id never
        # changes; only the LIVE job's state does). The identical-args loop
        # breaker doesn't know that and denies at 5 polls, ends the turn at
        # 8 -- verified: a `sleep 6` background job in `-p --output-format
        # stream-json` got reminders at polls 3-4, denials at 5-7, and the
        # turn ended at poll 8 with an empty result while the job was still
        # running, well before the job could ever finish. Exempt it
        # entirely (never counted, never denied/ended by this mechanism) --
        # a genuinely runaway poll loop is bounded by --max-turns instead,
        # the same higher-level guard that already bounds a paging loop
        # that never repeats identical arguments (see
        # test_max_turns_caps_model_calls_per_turn).
        key = (name, _canonical_args(tool_input))
        if name == "BashOutput":
            # exempt entirely -- never recorded in _loop_breaker, never
            # added to _loop_breaker_history (so it can't back-door into a
            # period-2 pattern with some other call either), count/
            # period2_count stay 0 so the threshold checks below are
            # always a no-op for it.
            count = 0
            period2_count = 0
        else:
            count = self._loop_breaker.get(key, 0) + 1
            self._loop_breaker[key] = count

            # H9 OpenCode item 20: period-2 ping-pong detection (see __init__'s
            # docstring comment on _loop_breaker_history/_loop_breaker_period2).
            history = self._loop_breaker_history
            period2_count = 0
            if len(history) >= 2 and history[-2] == key and history[-1] != key:
                # ORDERLESS pair (frozenset, not a tuple) so "..., A, B, A" and
                # the next step's "..., B, A, B" land in the SAME bucket --
                # without this, a clean A,B,A,B,... alternation would only
                # increment an (A-then-B) or (B-then-A) bucket every OTHER
                # step, growing no faster than the plain per-key counter above
                # and defeating the whole point of a dedicated pair detector.
                pair_key = frozenset((history[-1], key))
                period2_count = self._loop_breaker_period2.get(pair_key, 0) + 1
                self._loop_breaker_period2[pair_key] = period2_count
            history.append(key)
            if len(history) > 8:
                del history[:-8]  # bounded per turn; nothing downstream reads older entries

        item["count"] = count
        item["period2_count"] = period2_count
        effective = max(count, period2_count)
        if effective >= _LOOP_BREAKER_END_AT:
            if period2_count >= count:
                item["text"] = (f"Loop breaker: {name} is alternating with a previous call {period2_count} "
                                 f"times this turn (A, B, A, B, ...) -- ending the turn.")
            else:
                item["text"] = f"Loop breaker: {name} called with the same arguments {count} times this turn -- ending the turn."
            item["end_turn"] = True
            item["error_class"] = "loop_breaker"
            return item
        if effective >= _LOOP_BREAKER_DENY_AT:
            if period2_count >= count:
                item["text"] = (f"Loop breaker: {name} is alternating with a previous call {period2_count} "
                                 f"times -- denied. Try a different approach.")
            else:
                item["text"] = f"Loop breaker: {name} called with the same arguments {count} times -- denied. Try a different approach."
            item["error_class"] = "loop_breaker"
            return item

        if name in ("EnterPlanMode", "ExitPlanMode"):
            # H6 scope C: both are special-cased entirely in
            # `_dispatch_tools` (a mode switch / a plan-review round trip,
            # neither of which the generic permission-decide pipeline
            # models) -- this bypasses decide() outright rather than asking
            # "may this tool run" about a tool whose own body already
            # handles user confirmation on its own terms. AskUserQuestion
            # is DIFFERENT (see below): it DOES go through decide(), since
            # permissions.py's own mode table gives it real dontAsk-mode
            # semantics that must not be skipped.
            item["special"] = name
            item["ready"] = True
            return item

        tool = self.tool_registry.get(name)
        decision: Decision = self.permission_engine.decide(name, tool_input, tool=tool)

        # H4 scope B: PreToolUse fires AFTER decide(), before dispatch, for
        # EVERY call (must-do: for a run of batched read-only calls this
        # runs HERE, inside this per-call resolve step, which always
        # completes before `_dispatch_tools` ever decides to batch anything
        # -- verdicts are collected up front, never from inside the pool).
        # `updatedInput` replaces the input regardless of the permission
        # outcome; a hook `allow`/`ask` can skip/add a prompt the mode
        # table would otherwise have applied, but an explicit DENY rule
        # (decision.action == "deny" here already) is never overridden.
        if self.hook_runner is not None and self.hook_runner.has_hooks("PreToolUse"):
            payload = self.hook_runner.payload(
                "PreToolUse", prompt_id=f"turn_{self.turn_count}",
                extra={"tool_name": name, "tool_input": tool_input, "tool_use_id": tool_id},
            )
            pre_outcome = self._run_hook("PreToolUse", payload, matched=name, tool_name=name,
                                           tool_input=tool_input, tool=tool, abort=self.abort)
            for msg in pre_outcome.system_messages:
                item.setdefault("hook_system_messages", []).append(msg)
            if pre_outcome.updated_input is not None:
                tool_input = pre_outcome.updated_input
                item["input"] = tool_input
                # finding 13 (major, h4-h5-h3c review): re-run the deny/ask
                # gate against the REWRITTEN input -- `decision` above was
                # computed against the ORIGINAL input, before this hook
                # ever ran; a hook that only rewrites the command (and
                # never sets its own `permission_decision`) must not let
                # that stale decision silently survive into a command that
                # NOW matches one of the user's own deny/ask rules (deny
                # rules are the only gate this harness keeps -- a rewrite
                # must never be able to route around one). The very next
                # `if pre_outcome.permission_decision and decision.action
                # != "deny":` block below still lets an EXPLICIT hook
                # decision override this fresh one, same as before, except
                # a genuine deny match on the NEW input now correctly wins
                # over it either way.
                decision = self.permission_engine.decide(name, tool_input, tool=tool)
            if pre_outcome.blocked:
                item["text"] = f"Blocked by a PreToolUse hook: {pre_outcome.block_reason or 'blocked'}"
                item["permission_denial"] = {"tool_name": name, "tool_input": tool_input,
                                              "reason": pre_outcome.block_reason or "blocked by a PreToolUse hook",
                                              "suggested_rule": None}
                return item
            if pre_outcome.permission_decision and decision.action != "deny":
                reason = pre_outcome.permission_decision_reason or f"{pre_outcome.permission_decision}ed by a PreToolUse hook"
                if pre_outcome.permission_decision in ("allow", "deny", "ask"):
                    decision = Decision(pre_outcome.permission_decision, reason, source="hook")
                # "defer": leave `decision` exactly as decide() computed it.

        if decision.action == "deny":
            text = f"Permission denied: {decision.reason}"
            if decision.suggested_rule:
                text += f" (suggested rule: {decision.suggested_rule})"
            item["text"] = text
            item["permission_denial"] = decision.permission_denial or {
                "tool_name": name, "tool_input": tool_input, "reason": decision.reason, "suggested_rule": decision.suggested_rule,
            }
            self._fire_permission_denied(name, tool_input, decision.reason)
            return item
        if decision.action == "ask":
            # H4 scope B: PermissionRequest fires before the UI card would
            # ever be shown -- a hook's own answer wins outright (D-CFG).
            if self.hook_runner is not None and self.hook_runner.has_hooks("PermissionRequest"):
                pr_payload = self.hook_runner.payload(
                    "PermissionRequest", extra={"tool_name": name, "tool_input": tool_input,
                                                 "tool_use_id": tool_id, "permission_suggestions": None},
                )
                pr_outcome = self._run_hook("PermissionRequest", pr_payload, matched=name, tool_name=name,
                                              tool_input=tool_input, tool=tool, abort=self.abort)
                for msg in pr_outcome.system_messages:
                    item.setdefault("hook_system_messages", []).append(msg)
                if pr_outcome.updated_input is not None:
                    # H5c finding 9: apply `updatedInput`, THEN re-run
                    # decide() against it -- same pattern (and same
                    # reasoning) as PreToolUse's own rewrite above: a
                    # decision computed against the ORIGINAL input must
                    # never silently survive a rewrite that now matches one
                    # of the user's own deny/ask rules.
                    tool_input = pr_outcome.updated_input
                    item["input"] = tool_input
                    decision = self.permission_engine.decide(name, tool_input, tool=tool)
                # H5c finding 9: `updatedPermissions` (2.1.281's real LIST
                # shape -- hooks.py's own `_apply_updated_permissions_list`
                # already rejected anything else with a warning) applies
                # regardless of this call's own allow/deny outcome, exactly
                # like a live PermissionCard's "session" answer would.
                if pr_outcome.set_mode:
                    self.permission_engine.mode = pr_outcome.set_mode
                for rule_update in pr_outcome.updated_permissions:
                    self.permission_engine.add_session_rule(rule_update["rule"], rule_update.get("behavior", "allow"))
                if decision.action != "deny" and pr_outcome.permission_decision in ("allow", "deny"):
                    reason = pr_outcome.permission_decision_reason or f"{pr_outcome.permission_decision}ed by a PermissionRequest hook"
                    decision = Decision(pr_outcome.permission_decision, reason, source="hook")
                if pr_outcome.continue_ is False:
                    # H5c finding 9: `interrupt: true` ends the WHOLE TURN,
                    # not just this one call -- reusing the SAME `self.abort`
                    # signal Esc itself sets means every existing check
                    # already threaded through dispatch/streaming reacts
                    # with no new plumbing: this call (and every remaining
                    # one in this batch) is synthesized as not-run by
                    # `_dispatch_tools`'s own top-of-loop abort check, and
                    # `_turn_body` reports the turn `reason="interrupted"`.
                    self.abort.set()
                    text = (pr_outcome.permission_decision_reason
                            or "Permission denied and the turn was interrupted by a PermissionRequest hook")
                    item["text"] = text
                    item["permission_denial"] = {"tool_name": name, "tool_input": tool_input,
                                                  "reason": text, "suggested_rule": None}
                    self._fire_permission_denied(name, tool_input, text)
                    return item
            if decision.action == "deny":
                text = f"Permission denied: {decision.reason}"
                item["text"] = text
                item["permission_denial"] = decision.permission_denial or {
                    "tool_name": name, "tool_input": tool_input, "reason": decision.reason, "suggested_rule": None,
                }
                self._fire_permission_denied(name, tool_input, decision.reason)
                return item
            if decision.action == "ask":
                item["ask_reason"] = decision.reason
                # H5c finding 8 (closes the H6 v1 gap / D10 must-do): a
                # FOREGROUND sub-agent of an interactive parent ALSO takes
                # the live, blocking path below -- `_subagent_live_asks`
                # (set by `agent/subagent.py`'s `_build_child_session`,
                # only when the parent is interactive and this child is
                # not backgrounded) plus a `_permission_waiters` dict
                # SHARED with the parent (same pattern as the shared
                # `abort` Event) is what makes this safe: the parent's own
                # `resolve_permission` (the UI's `Controller.
                # answer_permission`) already reads from that exact dict,
                # so it can resolve a CHILD's waiter with no changes on
                # its side. This only works because `_run_child_to_
                # completion`/`_run_agent_batch_live` now stream every
                # child event LIVE (an `on_event` callback, never buffered
                # until the whole child turn ends) -- the `permission_
                # request` this method is about to yield reaches the
                # parent's live stream (and therefore the TUI) BEFORE the
                # child's own thread blocks on the waiter below.
                if not (self.interactive or self._subagent_live_asks):
                    # W4a `--permission-prompt-tool`/`--permission-prompts`:
                    # consulted BEFORE the immediate-denial fallback below --
                    # "host" (the default once a tool is configured) routes
                    # the ask to it; "none" (or no tool configured) skips
                    # straight to the existing denial, matching claude's own
                    # "none: nobody, anything that would prompt is denied
                    # automatically" wording exactly.
                    if self.permission_prompt_tool and self.permission_prompts != "none":
                        ppt_result = self._ask_permission_prompt_tool(name, tool_input, decision.reason)
                        if ppt_result == "allow":
                            item["ready"] = True
                            return item
                        if ppt_result == "deny":
                            item["text"] = f"Permission denied by --permission-prompt-tool: {decision.reason}"
                            item["permission_denial"] = {
                                "tool_name": name, "tool_input": tool_input,
                                "reason": decision.reason, "suggested_rule": decision.suggested_rule,
                            }
                            self._fire_permission_denied(name, tool_input, decision.reason)
                            return item
                        # None (tool missing/unreachable/inconclusive reply):
                        # falls through to the ordinary denial below, same
                        # as claude's own "none" target.
                    # no UI is attached (print mode / a bare Session in a
                    # test), OR a BACKGROUND sub-agent (its events are never
                    # forwarded to any live stream, so a live card for it
                    # would have nowhere to be shown and would just hang):
                    # resolve immediately as a denial, H2b's behaviour.
                    # `item["permission_denial"]` is populated here too
                    # (previously only the "deny" paths were) -- this
                    # session's own `permission_denials` list is what
                    # surfaces it, agent-tagged, to the user: print mode via
                    # the top-level result's own field (`run_agent_call`
                    # merges a completed child's list into the parent's).
                    item["text"] = f"Permission requires interactive approval, unavailable in this session: {decision.reason}"
                    item["permission_denial"] = decision.permission_denial or {
                        "tool_name": name, "tool_input": tool_input, "reason": decision.reason,
                        "suggested_rule": decision.suggested_rule,
                    }
                    self._fire_permission_denied(name, tool_input, decision.reason)
                    return item
                # interactive (or a live-asking sub-agent): `_dispatch_tools`
                # yields the `permission_request` and then BLOCKS on this
                # waiter until the UI answers (U2).
                item["pending_ask"] = True
                item["suggested_rule"] = decision.suggested_rule
                # H9 critical review finding 2: namespace the waiter key
                # (and the `permission_request` event's own "id", set from
                # this same value below) by `agent_id` for a sub-agent --
                # `_permission_waiters` is the SAME dict object shared with
                # the parent (agent/subagent.py's `_build_child_session`),
                # so without this, two PARALLEL children whose own
                # tool_use ids happen to collide (Kimi's per-call counter
                # restarting at 0 in each child's own, separately-empty
                # log is the concrete repro) overwrite each other's live
                # slot: the LOSING child's worker thread waits forever on
                # an Event nobody will ever set, and answering the
                # surviving card silently resolves the WRONG child.
                request_id = f"{self.agent_id}:{tool_id}" if self.agent_id else tool_id
                if request_id in self._permission_waiters:
                    # Never overwrite a live slot outright (belt-and-
                    # suspenders past the namespacing above, e.g. the same
                    # child asking again with the same id before its first
                    # ask was ever answered) -- disambiguate instead of
                    # silently discarding whoever is still waiting on it.
                    suffix = 2
                    while f"{request_id}#{suffix}" in self._permission_waiters:
                        suffix += 1
                    request_id = f"{request_id}#{suffix}"
                item["ask_request_id"] = request_id
                # 1.0.1 fixpass finding 4: `tool_name`/`tool_input`/`tool`
                # carried on the slot itself (not just the local `item`,
                # which lives only in this worker-thread frame) so the UI
                # thread's `reevaluate_pending_permission` below can re-run
                # `permission_engine.decide()` for this SAME parked request
                # under a newly-changed mode without reaching into the
                # worker's own stack.
                self._permission_waiters[request_id] = {
                    "event": threading.Event(), "decision": None,
                    "tool_name": name, "tool_input": tool_input, "tool": tool,
                }
                return item
            # decision.action == "allow" (the PermissionRequest hook just
            # answered it) -- fall through to item["ready"]=True below.
            # (decision.action == "deny" is unreachable here -- the plain
            # `if decision.action == "deny":` above already caught and
            # returned for every path that could produce it, including a
            # PreToolUse hook's own reassignment, which runs before it.)

        if name == "AskUserQuestion" and (self.interactive or self._subagent_live_asks):
            # U2 + finding "AskUserQuestion dontAsk gap": reached only once
            # decide() (and any PreToolUse hook) has already confirmed this
            # ISN'T dontAsk mode -- permissions.py's own mode table denies
            # AskUserQuestion outright in dontAsk (caught by the plain
            # `if decision.action == "deny":` above, well before here), so
            # dontAsk mode never shows a card at all, matching Claude
            # Code's own "AskUserQuestion errors on a tool call in dontAsk
            # mode" behavior. In every OTHER mode the question IS the
            # interaction, never itself a permission decision: park this
            # call on the UI's reply instead of dispatching to the tool's
            # own run() (which stays print mode's error path -- this
            # branch only runs when a UI is actually attached).
            #
            # W4a: `self._subagent_live_asks` (same flag/sharing `_build_
            # child_session` already sets up for `_permission_waiters`, see
            # that module's own docstring) extends this to a sub-agent of an
            # interactive parent -- foreground OR background, now that
            # `_question_waiters` is ALSO shared with the parent (agent/
            # subagent.py) and namespaced by agent_id here exactly like the
            # permission-ask branch above already is, for the same reason
            # (two parallel/sequential children's own tool_use ids can
            # collide).
            request_id = f"{self.agent_id}:{tool_id}" if self.agent_id else tool_id
            item["pending_question"] = True
            item["question_request_id"] = request_id
            self._question_waiters[request_id] = {"event": threading.Event(), "answer": None}
            return item

        item["ready"] = True
        return item

    def _fire_cwd_changed_if_moved(self) -> None:
        """W4a: CwdChanged -- "the persistent Bash cd moved the cwd"
        (removed from NOT_EMITTED_V1). `self._bash_state["cwd"]` is the SAME
        dict `tools/bash.py` updates in place after a `cd` (scope A: "cd
        persistence per session") -- compared against the last value this
        method itself saw, so only a REAL move fires, never every Bash call."""
        current = self._bash_state.get("cwd")
        if current is None or str(current) == str(self._last_known_bash_cwd):
            return
        previous = self._last_known_bash_cwd
        self._last_known_bash_cwd = current
        if self.hook_runner is None or not self.hook_runner.has_hooks("CwdChanged"):
            return
        payload = self.hook_runner.payload("CwdChanged", extra={"cwd": str(current), "previous_cwd": str(previous)})
        try:
            self._run_hook("CwdChanged", payload, matched=str(current))
        except Exception:
            pass

    def _fire_file_changed(self, name: str, tool_input: dict) -> None:
        """W4a: FileChanged -- "an Edit/Write/NotebookEdit landed" (removed
        from NOT_EMITTED_V1). Observational only, fired AFTER the tool
        already succeeded (same timing as PostToolUse, right next to it) --
        nothing it returns changes anything, there is nothing left to
        un-write."""
        if self.hook_runner is None or not self.hook_runner.has_hooks("FileChanged"):
            return
        file_path = tool_input.get("notebook_path") if name == "NotebookEdit" else tool_input.get("file_path")
        payload = self.hook_runner.payload("FileChanged", extra={"tool_name": name, "file_path": file_path})
        try:
            self._run_hook("FileChanged", payload, matched=name)
        except Exception:
            pass

    def _ask_permission_prompt_tool(self, name: str, tool_input: dict, reason: str) -> Optional[str]:
        """W4a `--permission-prompt-tool <tool>`: `<tool>` is an MCP tool's
        full catalog name (`mcp__<server>__<tool>`, exactly as it appears in
        `/mcp`/the wire catalog); called with `{tool_name, tool_input,
        reason}` and expected to reply with `{"behavior": "allow"|"deny"}`
        JSON (a bare `allow`/`deny` text reply is also accepted). Returns
        None -- never raises -- for anything else (malformed name, no MCP
        manager, the call itself erroring, an inconclusive reply): the
        caller's own existing denial fallback is what the user actually
        sees for that case, same as having no --permission-prompt-tool at
        all."""
        tool_name = self.permission_prompt_tool or ""
        parts = tool_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp" or self.mcp_manager is None:
            return None
        _prefix, server, mcp_tool = parts
        try:
            result = self.mcp_manager.call(server, mcp_tool,
                                            {"tool_name": name, "tool_input": tool_input, "reason": reason},
                                            timeout=30)
        except Exception:
            return None
        if bool(getattr(result, "isError", False) or getattr(result, "is_error", False)):
            return None
        text = "\n".join(b.text for b in (getattr(result, "content", None) or []) if isinstance(getattr(b, "text", None), str)).strip()
        parsed = None
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        behavior = parsed.get("behavior") if isinstance(parsed, dict) else text.lower()
        return behavior if behavior in ("allow", "deny") else None

    def _fire_permission_denied(self, name: str, tool_input: dict, reason: str) -> None:
        """H4 scope B: PermissionDenied -- observational only (nothing it
        returns can change an already-final denial)."""
        if self.hook_runner is None or not self.hook_runner.has_hooks("PermissionDenied"):
            return
        payload = self.hook_runner.payload("PermissionDenied", extra={"tool_name": name, "tool_input": tool_input,
                                                                        "reason": reason})
        try:
            self._run_hook("PermissionDenied", payload, matched=name, tool_name=name, tool_input=tool_input)
        except Exception:
            pass

    def _post_tool_use_response_value(self, content) -> object:
        """A JSON-safe `tool_response` payload value from a ToolResult's
        raw `.content` -- a string passes through; a block list is
        flattened to its text (images/other blocks noted by type, never
        dropped silently); anything else becomes its `str()`."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif isinstance(b, dict):
                    parts.append(f"[{b.get('type', 'content')} block]")
                else:
                    parts.append(str(b))
            return "\n".join(parts)
        return str(content)

    def _apply_post_tool_use_hooks(self, name: str, tool_id: str, tool_input: dict, tr) -> "tuple[object, list]":
        """H4 scope B/must-do: PostToolUse/PostToolUseFailure, BEFORE
        `spill_and_truncate`/`_mcp_blocks_for_log` ever run (the hook sees
        the model's real, full, untruncated output). Returns `(tr,
        system_messages)` -- `tr` is a NEW ToolResult when a hook added
        `additionalContext` or hard-blocked (appended as a trailing note;
        a block also flips `is_error=True`, since the tool already ran and
        there is nothing left to "not run"), else the original unchanged."""
        event = "PostToolUseFailure" if tr.is_error else "PostToolUse"
        if self.hook_runner is None or not self.hook_runner.has_hooks(event):
            return tr, []
        response_value = self._post_tool_use_response_value(tr.content)
        if event == "PostToolUseFailure":
            payload = self.hook_runner.payload(
                event, extra={"tool_name": name, "tool_input": tool_input, "tool_use_id": tool_id,
                               "error": response_value, "error_type": "tool_error", "is_interrupt": False},
            )
        else:
            payload = self.hook_runner.payload(
                event, extra={"tool_name": name, "tool_input": tool_input, "tool_use_id": tool_id,
                               "tool_response": response_value},
            )
        tool = self.tool_registry.get(name)
        # H5c finding 12: `abort` threaded through here too -- Esc during a
        # slow PostToolUse/PostToolUseFailure command hook used to be
        # completely ignored (no way to notice Esc until the hook's own
        # (default 600s) timeout elapsed).
        outcome = self._run_hook(event, payload, matched=name, tool_name=name, tool_input=tool_input, tool=tool,
                                  abort=self.abort)
        extra_note = ""
        if outcome.blocked:
            extra_note = f"\n\n[PostToolUse hook: {outcome.block_reason or 'blocked'}]"
        elif outcome.additional_context:
            extra_note = f"\n\n{outcome.additional_context}"
        if not extra_note:
            return tr, outcome.system_messages
        if isinstance(tr.content, list):
            new_content = tr.content + [{"type": "text", "text": extra_note.strip()}]
        else:
            new_content = (tr.content if isinstance(tr.content, str) else str(tr.content)) + extra_note
        return ToolResult(content=new_content, is_error=(tr.is_error or outcome.blocked)), outcome.system_messages

    def _finalize_tool_result(self, turn_no: int, item: dict, session_dir) -> Iterator[events.Event]:
        """Log + yield the `tool_result` for one `_resolve_tool_call` item,
        whether it was rejected before ever reaching a tool or actually
        ran (solo or inside a read-only batch, `item["result"]` either way
        by the time this is called). H11b finding 3/7: also RETURNS
        `(content, is_error)` (via the generator's own return value,
        `yield from`-visible) -- `content` is `text` for a rejected item,
        else whatever got logged (a block list when the tool's own result
        was blocks, e.g. a vision image, else the capped text) -- so
        `agent/cc_runtime.py`'s bridge can reuse this ONE method for every
        bridged tool call (plan tools included) and build the wire MCP
        reply from its return value instead of duplicating any of the
        hook/spill/logging logic here."""
        tool_id, name = item["tool_id"], item["name"]
        if not item["ready"]:
            text = item["text"]
            # H10 Part A: explicit `item["error_class"]` (schema_invalid,
            # loop_breaker, "other" for a duplicate/unknown-tool) wins;
            # otherwise a permission_denial marks "denied_by_rule", an
            # in-flight abort marks "interrupted" (Esc/steer-preemption
            # both set this before a synthesized result reaches here), and
            # anything left over (a dismissed AskUserQuestion, ...) is "other".
            error_class = item.get("error_class")
            if error_class is None:
                if item.get("permission_denial") is not None:
                    error_class = "denied_by_rule"
                elif self.abort.is_set():
                    error_class = "interrupted"
                else:
                    error_class = "other"
            self.log.append_tool_result(tool_use_id=tool_id, content=text, is_error=True,
                                         tool=name, error_class=error_class,
                                         num_bytes=len(text.encode("utf-8", errors="replace")) if isinstance(text, str) else None)
            yield events.Event("tool_result", {"id": tool_id, "ok": False, "summary": text}, turn=turn_no)
            if item.get("permission_denial") is not None:
                self.permission_denials.append(item["permission_denial"])
            return text, True
        tr = item["result"]
        tr, hook_system_messages = self._apply_post_tool_use_hooks(name, tool_id, item["input"], tr)
        for msg in hook_system_messages:
            yield events.notification(msg)
        if name in ("Edit", "Write", "NotebookEdit") and not tr.is_error:
            self._fire_file_changed(name, item["input"])
        if name == "Bash":
            self._fire_cwd_changed_if_moved()
        if isinstance(tr.content, list):
            # finding 5/H4 must-do: logged AS BLOCKS (vision images stay
            # real image blocks; tool_reference stripped) and capped HERE,
            # after the tool ran -- see _mcp_blocks_for_log's own docstring.
            tool = self.tool_registry.get(name)
            meta = getattr(tool, "meta", None)
            content_for_log = _mcp_blocks_for_log(tr.content, meta=meta, session_dir=session_dir, tool_use_id=tool_id)
            summary_text = _summary_text_for_blocks(content_for_log)
        elif isinstance(tr.content, str):
            content_for_log = spill_and_truncate(tr.content, cap=self.tool_registry.result_cap(name),
                                                  session_dir=session_dir, tool_use_id=tool_id)
            summary_text = content_for_log
        else:
            content_for_log = spill_and_truncate(str(tr.content), cap=self.tool_registry.result_cap(name),
                                                  session_dir=session_dir, tool_use_id=tool_id)
            summary_text = content_for_log

        count = item.get("count")
        period2_count = item.get("period2_count", 0)
        effective = max(count, period2_count) if count is not None else 0
        if count is not None and _LOOP_BREAKER_REMIND_AT <= effective < _LOOP_BREAKER_DENY_AT:
            if period2_count >= count:
                reminder = (f"\n\n[reminder: {name} has now alternated with a previous call {period2_count} "
                            f"times this turn (A, B, A, B, ...) -- consider a different approach if unintentional]")
            else:
                reminder = (f"\n\n[reminder: {name} has now been called with these same arguments "
                            f"{count} times this turn -- consider a different approach if unintentional]")
            if isinstance(content_for_log, list):
                content_for_log = content_for_log + [{"type": "text", "text": reminder.strip()}]
            else:
                content_for_log += reminder
            summary_text += reminder
        # H10 Part A: `spilled` detects tools/truncate.py's and
        # tools/mcp_tool.py's own shared spill-pointer wording (both write
        # the exact same "Full output saved to <path>." sentence) rather
        # than re-deriving "did this get capped" from lengths, which would
        # need the PRE-cap text this function no longer has by this point.
        spilled = _SPILL_MARKER in summary_text if isinstance(summary_text, str) else False
        error_class = (_classify_tool_error_text(name, summary_text) if tr.is_error else None)
        # W3b model quality (RECOMMENDATIONS.md section 8): DeepSeek V4.1
        # Flash's 8% Edit failure rate is almost entirely "Found multiple
        # matches" from too little old_string context. The SAME per-family
        # hint the Edit tool's own description already carries once, up
        # front (edit_hint_for, H12 Part C) is reinforced HERE, appended to
        # the actual failing result -- right when it matters, rather than
        # relying on a model deep in a long turn still attending to text it
        # read once at session start. Never for Claude/GPT (no family hint
        # configured at all -- see edit_hint_for's own docstring) and never
        # for any other error kind.
        if error_class == "multiple_matches" and name == "Edit":
            from halo_harness.providers.profiles import edit_hint_for
            hint = edit_hint_for(self.model_ref.provider, self.model_ref.model)
            if hint:
                reminder = f"\n\n[hint: {hint}]"
                if isinstance(content_for_log, list):
                    content_for_log = content_for_log + [{"type": "text", "text": reminder.strip()}]
                else:
                    content_for_log += reminder
                summary_text += reminder
        # H13 Part B ("inline images in the terminal"): the real (already
        # capped/spilled by _mcp_blocks_for_log above -- never the raw
        # uncapped tool output) base64 image data, extracted from
        # content_for_log so the TUI can attempt an inline render instead of
        # just the plain _image_caption text every event already carries
        # via summary/content. None when this result has no real image
        # block at all (the overwhelming majority of tool calls) -- kept
        # OUT of the event entirely rather than an empty list, so a caller
        # that only checks truthiness never has to special-case "present
        # but empty".
        images_for_event = [
            {"media_type": b["source"].get("media_type"), "data": b["source"].get("data")}
            for b in (content_for_log if isinstance(content_for_log, list) else [])
            if isinstance(b, dict) and b.get("type") == "image" and isinstance(b.get("source"), dict)
            and b["source"].get("type") == "base64" and b["source"].get("data")
        ] or None
        self.log.append_tool_result(
            tool_use_id=tool_id, content=content_for_log, is_error=tr.is_error, tool=name,
            error_class=error_class, ms=tr.duration_ms,
            num_bytes=len(summary_text.encode("utf-8", errors="replace")) if isinstance(summary_text, str) else None,
            spilled=spilled,
        )
        # review finding 15: `summary` alone (200 chars) is what Ctrl+O's
        # pager showed too, since `set_result` used to overwrite the
        # card's `body_text` with it -- `content` carries the FULLER
        # (still capped by spill_and_truncate/_mcp_blocks_for_log above,
        # e.g. tens of KB, never the raw uncapped tool output) text so
        # the pager has something real to show.
        yield events.Event("tool_result", {"id": tool_id, "ok": not tr.is_error, "summary": summary_text[:200],
                                            "content": summary_text, "images": images_for_event}, turn=turn_no)
        return content_for_log, tr.is_error

    # ---- interactive permission handshake (U2) ---------------------------

    def resolve_permission(self, request_id: str, decision) -> bool:
        """Called from the UI THREAD: answer the `permission_request` for
        `request_id`, unblocking the worker thread parked in
        `_await_permission_decision`. `decision` is a
        `halo_harness.permissions.Decision` or a `{"action": ...}` dict
        (repaired by `set_permission_mode`/mode changes before this). Returns
        False when no such request is waiting (already answered/aborted)."""
        slot = self._permission_waiters.get(request_id)
        if slot is None:
            return False
        slot["decision"] = decision
        slot["event"].set()
        return True

    def reevaluate_pending_permission(self, request_id: str) -> Optional[str]:
        """1.0.1 fixpass finding 4: called from the UI THREAD on a mode
        change (Shift+Tab/`/permissions`) while `request_id` is still
        parked in `_await_permission_decision` -- re-runs `permission_
        engine.decide()` for the SAME `tool_name`/`tool_input`/`tool` the
        ask was originally raised with (stashed on the waiter slot itself
        at creation time, see `_resolve_tool_call`'s own ask branch), now
        against `permission_engine.mode`'s NEW value.

        Resolves the waiter (exactly like `resolve_permission` above) and
        returns the resulting action ("allow"/"deny") ONLY when `decide()`
        no longer says "ask" -- e.g. `auto` allows most things but an
        explicit `ask:` rule still asks in every mode but
        `bypassPermissions`, so cycling to `auto` must not blindly approve
        that. Returns None (the waiter is left untouched, still parked)
        when nothing is waiting for `request_id`, or `decide()` still says
        "ask" under the new mode -- the caller must leave the card up
        either way, never guess at a UI-level shortcut."""
        slot = self._permission_waiters.get(request_id)
        if slot is None:
            return None
        tool_name = slot.get("tool_name")
        if tool_name is None:
            return None
        decision = self.permission_engine.decide(tool_name, slot.get("tool_input"), slot.get("tool"))
        if decision.action not in ("allow", "deny"):
            return None
        slot["decision"] = decision
        slot["event"].set()
        return decision.action

    def resolve_question(self, request_id: str, answer) -> bool:
        """Called from the UI THREAD: answer a pending `question` (the
        AskUserQuestion round trip), unblocking the worker parked in
        `_await_reply`. `answer` is whatever the UI produced -- a plain
        string, or a list/dict of answers, JSON-encoded into the tool
        result. Returns False when nothing is waiting for `request_id`."""
        slot = self._question_waiters.get(request_id)
        if slot is None:
            return False
        slot["answer"] = answer
        slot["event"].set()
        return True

    def resolve_plan(self, decision) -> bool:
        """Called from the UI THREAD (`Controller.answer_plan`, DIRECTLY --
        never through the command queue, same reasoning as
        `resolve_permission`/`resolve_question`: the worker is parked
        inside the running turn's `ExitPlanMode` wait, not polling
        `commands`): answer the ONE pending plan review. `decision` is a
        `{"approved": bool, "feedback": str, "mode_after": str|None}` dict
        (PlanCard's own shape). Plan mode never has more than one pending
        review at a time (a whole turn blocks on ExitPlanMode), so unlike
        `resolve_permission`/`resolve_question` there is no caller-supplied
        request_id to match against -- `self._pending_plan_id` (set by
        `_dispatch_tools` right before it starts waiting) is authoritative.
        Returns False when nothing is waiting."""
        request_id = self._pending_plan_id
        if request_id is None:
            return False
        slot = self._plan_waiters.get(request_id)
        if slot is None:
            return False
        slot["decision"] = decision
        slot["event"].set()
        return True

    def _apply_pending_agent_notices(self, turn_no: int):
        """Pop every queued background-sub-agent-completion notice and
        apply each as a user-role message (H6 scope F / dsh: "background
        jobs ... report completion as a user-role notice in the next
        step") -- called once at the START of `_turn_body`, so the model
        sees any sub-agent that finished while this session was between
        turns (or during a PRIOR turn's own tool dispatch) before it does
        anything else this turn."""
        with self._agent_notices_lock:
            notices, self._pending_agent_notices = self._pending_agent_notices, []
        for text in notices:
            self.log.append_user([{"type": "text", "text": text}], kind="agent_notice")
            yield events.user_message(text, turn=turn_no)
            yield events.notification(f"Sub-agent finished: {text.splitlines()[0]}")

    def _apply_pending_job_notices(self, turn_no: int):
        """H8 scope A: the background-Bash-job sibling of
        `_apply_pending_agent_notices` (same dsh rule, same "called once at
        the START of `_turn_body`" timing) -- a job that finished while this
        session was between turns (or during a prior turn's own tool
        dispatch) is applied as a user-role message before the model does
        anything else this turn."""
        with self._job_notices_lock:
            notices, self._pending_job_notices = self._pending_job_notices, []
        for text in notices:
            self.log.append_user([{"type": "text", "text": text}], kind="job_notice")
            yield events.user_message(text, turn=turn_no)
            yield events.notification(f"Background job finished: {text.splitlines()[0]}")

    def _await_reply(self, waiters: dict, request_id: str, *, timeout: Optional[float] = None):
        """Block the WORKER thread until a UI-thread `resolve_*` call answers
        `request_id` or the session's abort Event is set (the escape hatch:
        Esc/Ctrl+C during a pending prompt). Returns the stored value, or
        None on abort/timeout/never-registered.

        review finding 1 (critical): the slot MUST stay registered in
        `waiters` for the WHOLE wait -- popping it up front (the old bug)
        meant `resolve_permission`/`resolve_question` (called from the UI
        thread, which only ever does `waiters.get(request_id)`) found
        nothing, silently returned False, and every card answer was lost:
        the worker just sat here until Esc/quit fired `self.abort`. Popped
        in `finally`, once, after the wait actually ends."""
        slot = waiters.get(request_id)
        if slot is None:
            return None
        try:
            while not slot["event"].wait(0.1):
                if self.abort.is_set():
                    return None
                if timeout is not None:
                    timeout -= 0.1
                    if timeout <= 0:
                        return None
            return slot["answer"] if "answer" in slot else slot["decision"]
        finally:
            waiters.pop(request_id, None)

    def _await_permission_decision(self, request_id: str, *, timeout: Optional[float] = None):
        return self._await_reply(self._permission_waiters, request_id, timeout=timeout)

    def _apply_permission_decision(self, item: dict, decision) -> None:
        """Apply the UI's answer to a pending-`ask` item IN PLACE: `allow`
        makes it ready to dispatch (and teaches the engine a session rule
        when the answer carried one); `deny` / no answer at all becomes the
        same `item["text"]` a decide()-time denial would have produced."""
        item.pop("pending_ask", None)
        if isinstance(decision, dict):
            decision = Decision(action=decision.get("action", "deny"),
                                 reason=decision.get("reason", ""),
                                 rule=decision.get("rule"),
                                 message=decision.get("message", "") or "",
                                 permission_denial=decision.get("permission_denial"))
        if decision is None:
            item["text"] = (f"Permission request dismissed or interrupted "
                             f"({item.get('ask_reason', 'interactive approval required')})")
            return
        if getattr(decision, "action", None) == "allow":
            rule_text = getattr(decision, "rule", None)
            if rule_text:
                self.permission_engine.add_session_allow_rule(rule_text)
            item["ready"] = True
            return
        text = f"Permission denied: {getattr(decision, 'reason', '') or item.get('ask_reason', '')}"
        message = getattr(decision, "message", "") or ""
        if message:
            text += f"\nThe user said: {message}"
        item["text"] = text
        item["permission_denial"] = {"tool_name": item["name"], "tool_input": item["input"],
                                      "reason": getattr(decision, "reason", "") or item.get("ask_reason", ""),
                                      "suggested_rule": item.get("suggested_rule")}

    def _handle_enter_plan_mode(self, turn_no: int, item: dict) -> Iterator[events.Event]:
        """H6 scope C: model-initiated `EnterPlanMode` -- always succeeds
        (D-CFG: "allowed in -p"; interactive sessions get a `notification`
        rather than a full ask round trip, since the mode switch itself
        touches nothing). Ensures the session's plan file exists (reused
        across repeated EnterPlanMode calls in the same session) and logs
        `PLAN_MODE_NOTE` as a snapshot so the model's own next request
        carries the "research, then ExitPlanMode" instruction."""
        settings = getattr(self.session_context, "settings", None)
        plan_path = ensure_plan_file(self.cwd, settings, existing=self.permission_engine.plan_file)
        self.permission_engine.mode = "plan"
        self.permission_engine.set_plan_file(plan_path)
        self.log.append_snapshot([{"type": "text", "text": PLAN_MODE_NOTE}], kind="plan_mode")
        item["result"] = ToolResult(f"Entered plan mode. Plan file: {plan_path}\n\n{PLAN_MODE_NOTE}")
        yield events.notification(f"Plan mode entered (plan file: {plan_path})")
        yield events.status(phase="thinking", model=self.model_ref.raw, turn=turn_no, permission_mode="plan")

    def _handle_exit_plan_mode(self, turn_no: int, item: dict) -> Iterator[events.Event]:
        """H6 scope C: `ExitPlanMode(plan)` writes the plan file and either
        (interactive) emits `plan_review` and BLOCKS on `resolve_plan`
        (Controller.answer_plan's direct call, mirroring `resolve_permission`/
        `resolve_question`), or (print mode / a bare Session) resolves
        immediately: `-p` auto-approves only under acceptEdits/
        bypassPermissions, else the plan text itself becomes the turn's
        visible result (`_plan_as_final_text`, emitted as a synthetic
        assistant message by the caller right after this tool's result)."""
        tool_id = item["tool_id"]
        plan_text = (item.get("input") or {}).get("plan") or ""
        settings = getattr(self.session_context, "settings", None)
        plan_path = ensure_plan_file(self.cwd, settings, existing=self.permission_engine.plan_file)
        self.permission_engine.set_plan_file(plan_path)
        write_plan(plan_path, plan_text)

        if self.interactive:
            self._plan_waiters[tool_id] = {"event": threading.Event(), "decision": None}
            self._pending_plan_id = tool_id
            yield events.Event("plan_review", {"id": tool_id, "plan": plan_text, "path": str(plan_path)}, turn=turn_no)
            decision = self._await_reply(self._plan_waiters, tool_id)
            self._pending_plan_id = None
            if decision is None:
                item["result"] = ToolResult(
                    "The plan review was dismissed or the turn was interrupted; the plan was not approved.",
                    is_error=True,
                )
                return
            approved = bool(decision.get("approved"))
            feedback = (decision.get("feedback") or "").strip()
            if approved:
                mode_after = decision.get("mode_after") or "acceptEdits"
                self.permission_engine.mode = mode_after
                item["result"] = ToolResult("User approved the plan; implement it.")
            else:
                text = "The user did not approve the plan."
                if feedback:
                    text += f" Feedback: {feedback}"
                item["result"] = ToolResult(text, is_error=True)
            return

        if self.permission_engine.mode in ("acceptEdits", "bypassPermissions"):
            item["result"] = ToolResult("User approved the plan; implement it.")
            return
        # No interactive reviewer and the mode doesn't auto-approve: the
        # plan text itself IS the turn's answer (brief C: "else the plan
        # text is the result") -- end the turn right after this tool
        # result, with no further model call.
        item["result"] = ToolResult(plan_text)
        item["end_turn"] = True
        item["_plan_as_final_text"] = plan_text

    def _emit_plan_as_final_text(self, turn_no: int, plan_text: str) -> Iterator[events.Event]:
        """A synthetic assistant message carrying `plan_text` verbatim, so
        every sink (PrintModeSink/StreamJsonSink/the TUI transcript) shows
        the plan as the turn's final answer with zero sink-side special
        casing -- see `_handle_exit_plan_mode`'s own docstring."""
        self.log.append_assistant(content=[{"type": "text", "text": plan_text}], stop_reason="end_turn")
        yield events.message_start(turn=turn_no, model=self.model_ref.raw)
        yield events.text_delta(plan_text, turn=turn_no)
        yield events.message_end(turn=turn_no, stop_reason="end_turn", usage={})

    def _dispatch_tools(self, turn_no: int, tool_use_blocks: list, tool_call_flags: Optional[dict] = None,
                         *, repair_outcomes: Optional[list] = None) -> Iterator[events.Event]:
        """Runs every tool_use in this assistant turn, per call, in order:
        length/malformed short-circuit (finding 3) -> basic shape validate
        (agent/invariants.validate_tool_use) -> repair (agent/repair.py:
        name resolution, schema coerce, duplicate detection) -> loop
        breaker (rule 8) -> permission decide -- all via `_resolve_tool_call`
        -- then dispatch -> truncate (tools/truncate.py). A RUN of
        consecutive READY read-only calls is accumulated and dispatched
        together on `tools.registry.run_read_only_batch`'s 4-thread pool
        (results re-ordered back to call order); anything else (not ready,
        or ready but not read-only) breaks the run and dispatches/resolves
        immediately. `tool_use_ready` (and a would-be `permission_request`)
        is still yielded per-call, in ORIGINAL order, the instant each
        call's decision is known -- BEFORE any dispatch happens for it or
        anything after it -- so an interrupt right after the Nth
        `tool_use_ready` still guarantees nothing from N onward ever ran
        (must-do 5: interrupt correctness is not weakened by batching).
        `ctx` reuses the SAME read_cache/bash_state dicts for the whole
        session. Returns True (via the generator's return value) iff the
        turn should end now rather than call the model again."""
        tool_call_flags = tool_call_flags or {}
        from halo_harness.hooks import env_file_path
        ctx = ToolContext(cwd=self.cwd, read_cache=self._read_cache, abort=self.abort,
                           bash_state=self._bash_state, session_dir=self.log.dir / self.log.session_id,
                           registry=self.tool_registry, env=self.tool_env, catalog=self.session_catalog,
                           mcp_manager=self.mcp_manager, agent_runtime=self.agent_runtime,
                           job_registry=self.job_registry, vision=self.model_profile.vision,
                           session_allow_rule=lambda rule_text: self.permission_engine.add_session_allow_rule(
                               rule_text, temporary=True),
                           env_file=env_file_path(self.log.session_id),
                           permission_engine=self.permission_engine, effort=self.effort)
        # H10 Part A: `_turn_body` (this method's one real caller) now
        # computes this UP FRONT, so it can log `tool_meta` on the SAME
        # assistant node before this method ever runs, and passes it in --
        # a direct unit-test call with no `repair_outcomes=` still gets it
        # computed here exactly as before.
        if repair_outcomes is None:
            repair_outcomes = repair_assistant_turn(tool_use_blocks, self.tool_registry, catalog=self.session_catalog)

        end_turn = False
        pending_batch: list = []  # `item` dicts: a run of consecutive READY read-only calls, dispatch deferred
        agent_batch: list = []    # `item` dicts: a run of consecutive READY Agent/Task calls, <=4 concurrent (H6 scope B)

        def _run_agent_batch() -> Iterator[events.Event]:
            """H5c finding 8: replaces the old pair (`_dispatch_agent_batch`
            running the whole pool synchronously, THEN `_flush_agent_batch_
            items` yielding every child's BUFFERED events all at once) with
            ONE streaming generator: every child's events -- most
            importantly a `permission_request`, which the child's OWN
            thread then blocks on, waiting for a live answer -- now reach
            THIS generator's caller (and therefore the live TUI) the
            INSTANT they happen, via one worker thread per concurrent
            child pushing into a shared queue this generator drains as it
            runs (`ThreadPoolExecutor.submit`, not the old blocking `.map`,
            still capped at `MAX_CONCURRENT_AGENTS`). A child blocked
            waiting for its own permission answer could never finish
            "buffering" under the old scheme, so its ask would never have
            reached anything -- `agent/subagent.py`'s `_run_child_to_
            completion` streaming every event via an `on_event` callback
            (never buffering first) is what makes this whole fix possible.
            Each item's `tool_result` is finalized (in ORIGINAL order) only
            once every child in this batch has actually finished."""
            nonlocal end_turn
            from halo_harness.agent.subagent import MAX_CONCURRENT_AGENTS, run_agent_call
            from halo_harness.tools.base import ToolResult

            q: "queue.Queue" = queue.Queue()
            _DONE = object()

            def _run_one(it: dict) -> None:
                try:
                    # H5c finding 7: a call QUEUED behind the pool's
                    # `MAX_CONCURRENT_AGENTS` limit (a 5th Agent call when
                    # only 4 workers are free) must not start a child
                    # session at all once Esc/Ctrl+C already fired while it
                    # was waiting -- checked here, on the worker thread,
                    # right before it would otherwise begin (never before
                    # the pool even starts, which would also block the
                    # calls that DID already get a slot).
                    if self.abort.is_set():
                        it["result"] = ToolResult("Sub-agent not started: interrupted by the user.", is_error=True)
                        return
                    _, tr = run_agent_call(
                        runtime=self.agent_runtime, tool_id=it["tool_id"], tool_input=it["input"],
                        tool_name=it["name"], on_event=q.put,
                    )
                    it["result"] = tr
                except Exception as e:  # run_agent_call is documented "never raises" -- defense in depth anyway
                    log.exception("sub-agent dispatch failed for tool_id=%r", it.get("tool_id"))
                    it["result"] = ToolResult(f"Sub-agent dispatch failed: {type(e).__name__}: {e}", is_error=True)
                finally:
                    q.put((_DONE, it))

            with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_AGENTS, len(agent_batch))) as pool:
                for it in agent_batch:
                    pool.submit(_run_one, it)
                remaining = len(agent_batch)
                while remaining > 0:
                    got = q.get()
                    if isinstance(got, tuple) and len(got) == 2 and got[0] is _DONE:
                        remaining -= 1
                        continue
                    yield got

            for it in agent_batch:
                yield from self._finalize_tool_result(turn_no, it, ctx.session_dir)
                if it.get("_plan_as_final_text") is not None:
                    yield from self._emit_plan_as_final_text(turn_no, it["_plan_as_final_text"])
                if it.get("end_turn"):
                    end_turn = True

        def _dispatch_pending_batch() -> None:
            """Actually RUN every item in `pending_batch` (fills in each
            `item["result"]`) -- does NOT log/yield anything; the caller
            still has to `_finalize_tool_result` each one itself, in order,
            same as a solo dispatch. finding 5 must-do: each call gets its
            OWN `tool_use_id` (the 3rd tuple element) so a pooled MCP
            result that needs to spill doesn't collide/silently skip."""
            calls = [(it["name"], it["input"], it["tool_id"]) for it in pending_batch]
            results = run_read_only_batch(self.tool_registry, calls, ctx)
            # ruff B905: `results` is one ToolResult per `calls` entry, by
            # construction (run_read_only_batch's own contract) -- strict=
            # True turns any future violation of that into a loud crash
            # instead of a silently-truncated/misaligned result set.
            for it, tr in zip(pending_batch, results, strict=True):
                it["result"] = tr

        # ruff B905: repair_assistant_turn appends exactly one outcome per
        # input block, always (see its own docstring/loop) -- strict=True
        # documents that invariant instead of silently tolerating a future
        # mismatch.
        for i, (tu, outcome) in enumerate(zip(tool_use_blocks, repair_outcomes, strict=True)):
            if self.abort.is_set():
                # review finding 2: nothing FURTHER starts once
                # interrupted -- a call already dispatched (solo, or
                # inside an already-running batch) is allowed to finish;
                # this and every remaining call become synthesized
                # interrupted results instead of ever reaching decide()/
                # dispatch. Flush whatever was already announced first.
                if pending_batch:
                    _dispatch_pending_batch()
                    yield from self._fire_post_tool_batch(turn_no, pending_batch)
                    for batched_item in pending_batch:
                        yield from self._finalize_tool_result(turn_no, batched_item, ctx.session_dir)
                    pending_batch = []
                if agent_batch:
                    yield from _run_agent_batch()
                    agent_batch = []
                yield from self._synthesize_unrun_tool_results(
                    turn_no, tool_use_blocks[i:], ctx.session_dir, reason="Tool call interrupted by user",
                )
                return True

            if self._pending_steer():
                # H5c finding 6: a steer already queued BEFORE this
                # iteration starts (typed during the stream tail, during
                # `_maybe_auto_compact`, or during a PreToolUse/permission
                # hook for an EARLIER call in this same turn) must stop
                # every call from here on, INCLUDING whatever is sitting in
                # `pending_batch`/`agent_batch` -- unlike an Esc/abort,
                # those are never flushed: `dispatch()` was never actually
                # called for any of them (only accumulated, dispatch
                # deferred until the run breaks), so none of them count as
                # "a running tool" yet -- "running tools finish" (scope
                # 0(c)) only ever meant a call whose `dispatch()` genuinely
                # started. Each already got its own `tool_use_ready` when
                # first queued into the batch, so they are finalized
                # directly here (not via `_synthesize_unrun_tool_results`,
                # which would re-announce them with a second event).
                reason = "not run: the user sent a new message first"
                for batched_item in pending_batch:
                    batched_item["ready"] = False
                    batched_item["text"] = reason
                    yield from self._finalize_tool_result(turn_no, batched_item, ctx.session_dir)
                pending_batch = []
                for agent_item in agent_batch:
                    agent_item["ready"] = False
                    agent_item["text"] = reason
                    yield from self._finalize_tool_result(turn_no, agent_item, ctx.session_dir)
                agent_batch = []
                yield from self._synthesize_unrun_tool_results(
                    turn_no, tool_use_blocks[i:], ctx.session_dir, reason=reason,
                )
                break

            item = self._resolve_tool_call(tu, outcome, tool_call_flags)
            tool_id, name = item["tool_id"], item["name"]

            if self.abort.is_set():
                # finding 14 (major, h4-h5-h3c review): Esc that fired
                # WHILE `_resolve_tool_call` was running (most notably: a
                # slow PreToolUse/PermissionRequest hook -- both now poll
                # `abort` themselves mid-wait and return early, see
                # `run_command_hook`/`run_http_hook`'s own docstrings) must
                # stop THIS call from ever dispatching too, not just the
                # NEXT iteration's own top-of-loop check -- the old code
                # only checked `abort` there, so a hook that noticed the
                # interrupt and returned early still let the gated Write
                # run anyway.
                if pending_batch:
                    _dispatch_pending_batch()
                    yield from self._fire_post_tool_batch(turn_no, pending_batch)
                    for batched_item in pending_batch:
                        yield from self._finalize_tool_result(turn_no, batched_item, ctx.session_dir)
                    pending_batch = []
                if agent_batch:
                    yield from _run_agent_batch()
                    agent_batch = []
                yield from self._synthesize_unrun_tool_results(
                    turn_no, tool_use_blocks[i:], ctx.session_dir, reason="Tool call interrupted by user",
                )
                return True

            yield events.Event("tool_use_ready", {"id": tool_id, "name": name, "input": item["input"],
                                                    "repaired": item["repaired"]}, turn=turn_no)
            for msg in item.pop("hook_system_messages", None) or []:
                yield events.notification(msg)
            if "ask_reason" in item:
                # H9 critical review finding 2: the event's own "id" is
                # what the UI echoes back verbatim through `resolve_
                # permission` (tui/dispatch.py's `_show_permission_card`
                # reads `data.get("id")` as `request_id`), so it must be
                # the SAME (possibly agent_id-namespaced) key `_resolve_
                # tool_call` actually inserted into `_permission_waiters`
                # -- falls back to the bare `tool_id` for the immediate-
                # deny (non-blocking) path, which never inserts a waiter
                # at all, so there's nothing for it to collide with.
                yield events.Event("permission_request", {"id": item.get("ask_request_id", tool_id), "name": name,
                                                            "input": item["input"], "reason": item["ask_reason"],
                                                            "suggested_rule": item.get("suggested_rule")}, turn=turn_no)

            if item.get("pending_ask"):
                # U2: this really BLOCKS the worker thread until the UI's
                # PermissionCard answers (`answer_permission` ->
                # `resolve_permission`) or the abort Event fires.
                #
                # W3b item 11: start/end (turn-relative ms, same clock
                # `self._timeline` uses everywhere else) and the resolved
                # decision recorded around the real blocking call -- the
                # `permission_request` event just above marks only the ASK,
                # never how long the answer took or what it was.
                wait_start_ms = self._timeline.elapsed_ms()
                decision = self._await_permission_decision(item.get("ask_request_id", tool_id))
                decision_label = getattr(decision, "action", None) if decision is not None else "dismissed"
                self._timeline.record_permission_wait(wait_start_ms, self._timeline.elapsed_ms(), decision_label)
                self._apply_permission_decision(item, decision)

            if item.get("pending_question"):
                # U2: the AskUserQuestion round trip -- never dispatched to
                # the tool itself; the UI's answer becomes its result.
                # W4a: "id" is the (possibly agent_id-namespaced) request_id
                # `_resolve_tool_call` parked the waiter under, never a bare
                # tool_id -- see that branch's own comment.
                q_request_id = item.get("question_request_id", tool_id)
                yield events.Event("question", {"id": q_request_id, "name": name, "input": item["input"]}, turn=turn_no)
                answer = self._await_reply(self._question_waiters, q_request_id)
                item.pop("pending_question", None)
                if answer is None:
                    item["text"] = "The user did not answer (the question was dismissed or the turn interrupted)."
                else:
                    item["result"] = ToolResult(answer if isinstance(answer, str)
                                                  else json.dumps(answer, ensure_ascii=False, default=str))
                    item["ready"] = True

            special = item.get("special")
            if special == "EnterPlanMode":
                yield from self._handle_enter_plan_mode(turn_no, item)
            elif special == "ExitPlanMode":
                yield from self._handle_exit_plan_mode(turn_no, item)

            if item["ready"] and name in ("Agent", "Task"):
                agent_batch.append(item)
                continue  # deferred -- dispatched together (<=4 concurrent) when this run breaks

            if agent_batch:
                yield from _run_agent_batch()
                agent_batch = []

            if item["ready"] and self.tool_registry.is_read_only(name):
                pending_batch.append(item)
                continue  # deferred -- dispatched only when the run breaks (below) or at the end

            # This call breaks any read-only run in progress: run + finalize
            # the WHOLE pending batch first (those calls were already
            # announced earlier and never depend on anything after them),
            # THEN handle the current one the same way.
            if pending_batch:
                _dispatch_pending_batch()
                yield from self._fire_post_tool_batch(turn_no, pending_batch)
                for batched_item in pending_batch:
                    yield from self._finalize_tool_result(turn_no, batched_item, ctx.session_dir)
                    if batched_item.get("end_turn"):
                        end_turn = True
                pending_batch = []

            if item["ready"] and "result" not in item:
                # H3/H4 must-do: wire progress_cb -> tool_progress events.
                # Solo dispatch only (never the concurrent read-only pool
                # below, which shares ONE ctx across threads -- a per-call
                # mutable callback there would race); a fresh per-call
                # ToolContext, via dataclasses.replace, keeps read_cache/
                # bash_state/abort/session_dir/registry/env as the SAME
                # shared objects, only tool_use_id/progress_cb differ.
                # review finding 15: `progress_cb` is a plain sync callback
                # from INSIDE a blocking `dispatch()` call, so truly
                # streaming each chunk out as its own live event needs a
                # thread/queue this pass doesn't have time for -- the
                # bounded collector below at least keeps a 60MB-streaming
                # Bash command from holding millions of characters in RAM
                # for the whole run (the old plain list did exactly that,
                # verified: 8.6M characters retained for a 300k-character
                # final result), keeping only a head/tail sample.
                progress_chunks = _BoundedChunks()
                item_ctx = dataclasses.replace(ctx, tool_use_id=tool_id, progress_cb=progress_chunks.append)
                item["result"] = self.tool_registry.dispatch(name, item["input"], item_ctx)
                for chunk in progress_chunks.chunks:
                    yield events.Event("tool_progress", {"id": tool_id, "name": name, "text": chunk}, turn=turn_no)
            yield from self._finalize_tool_result(turn_no, item, ctx.session_dir)
            if item.get("_plan_as_final_text") is not None:
                yield from self._emit_plan_as_final_text(turn_no, item["_plan_as_final_text"])
            if item.get("end_turn"):
                end_turn = True
            if self._pending_steer():
                # review finding 6 / scope 0(c): checked per TOOL, not
                # just once per whole batch -- a steer that arrived while
                # this call was running stops any FURTHER call in this
                # same assistant turn from starting; the one that just
                # finished is kept (it already ran to completion).
                # `_turn_body`'s own post-dispatch check applies it.
                #
                # finding 3 (critical): the OLD code broke out of this loop
                # right here with NO result for tool_use_blocks[i+1:] --
                # `_apply_pending_steers_events` (called right after
                # `_dispatch_tools` returns) then appended the steer as the
                # very next user-role message, so the log carried an
                # assistant turn with unanswered tool_use blocks followed
                # directly by a new user turn: invariant rule 2 broken,
                # 400s on `ant:`/Databricks Claude routes forever after.
                # Every call that didn't get a chance to run is synthesized
                # an `is_error` result right here, before the break, same
                # as the abort branch above already does for an interrupt.
                yield from self._synthesize_unrun_tool_results(
                    turn_no, tool_use_blocks[i + 1:], ctx.session_dir,
                    reason="not run: the user sent a new message first",
                )
                break

        if pending_batch:
            _dispatch_pending_batch()
            yield from self._fire_post_tool_batch(turn_no, pending_batch)
            for batched_item in pending_batch:
                yield from self._finalize_tool_result(turn_no, batched_item, ctx.session_dir)
                if batched_item.get("end_turn"):
                    end_turn = True
        if agent_batch:
            yield from _run_agent_batch()
        return end_turn

    def _synthesize_unrun_tool_results(self, turn_no: int, remaining_blocks: list, session_dir, *,
                                        reason: str) -> Iterator[events.Event]:
        """finding 3 (major, h4-h5-h3c review): every `tool_use` block that
        never got a chance to run (the turn ended early -- an interrupt, or
        a steer noticed mid-dispatch) must still get a synthesized
        `is_error` `tool_result` right here, before `_dispatch_tools`
        returns -- otherwise the NEXT request violates "every tool_use has
        exactly one tool_result before the next assistant turn"
        (`agent/invariants.py`'s own rule 2) and `translate.py`'s
        `("call_2", "(no result)")` placeholder tells the model a write
        that never ran produced empty output; on `ant:`/Databricks Claude
        routes the next request 400s outright, and so does EVERY later
        turn in the process (`find_unpaired_tool_use_ids` only ever checks
        the LAST assistant node, so a gap here is never self-healed by a
        later call)."""
        for remaining_tu in remaining_blocks:
            r_id, r_name = remaining_tu.get("id"), remaining_tu.get("name")
            item = {"tool_id": r_id, "name": r_name, "input": remaining_tu.get("input") or {},
                     "ready": False, "text": reason, "repaired": False}
            yield events.Event("tool_use_ready", {"id": r_id, "name": r_name, "input": item["input"],
                                                    "repaired": False}, turn=turn_no)
            yield from self._finalize_tool_result(turn_no, item, session_dir)

    def _fire_post_tool_batch(self, turn_no: int, pending_batch: list) -> Iterator[events.Event]:
        """H4 scope B: PostToolBatch -- fires once for a whole dispatched
        read-only batch, observational (its own JSON output is not applied
        back to the individual results; each item's own PostToolUse/
        PostToolUseFailure -- fired from `_finalize_tool_result` right
        after this -- is what can annotate/block ONE result)."""
        if self.hook_runner is None or not self.hook_runner.has_hooks("PostToolBatch"):
            return
        tool_calls = [{
            "tool_name": it["name"], "tool_input": it["input"], "tool_use_id": it["tool_id"],
            "tool_response": self._post_tool_use_response_value(it["result"].content),
        } for it in pending_batch if it.get("result") is not None]
        if not tool_calls:
            return
        payload = self.hook_runner.payload("PostToolBatch", extra={"tool_calls": tool_calls})
        # H5c finding 12: `abort` threaded through -- Esc during a slow
        # PostToolBatch command hook used to be completely ignored.
        outcome = self._run_hook("PostToolBatch", payload, matched="", abort=self.abort)
        for msg in outcome.system_messages:
            yield events.notification(msg)

    # ---- the interactive command pump (U2, D-Contract) -------------------

    def set_model(self, model_ref: ModelRef, model_profile: ModelProfile, creds=None) -> None:
        """Swap the active model mid-session (`/model`): the ref, its
        profile and (when given) its credentials, plus the derived Route/
        provider profile every request builder reads.

        review finding 14: three things used to go silently stale here --
        (a) every already-loaded `McpTool.vision` flag (a switch away
        from a vision model kept sending logged screenshots as
        `image_url` parts a text-only endpoint rejects), (b) the
        SessionCatalog's tool-count CAP (OpenRouter 128 -> Databricks 32
        made every later request fail with `tool_catalog_too_large` once
        enough ToolSearch loads had accumulated past the new, smaller
        cap) -- fixed by re-gating vision and LRU-evicting down to fit,
        and (c) no log record of the switch at all -- a `meta` node now
        carries the new model + the (possibly just-shrunk) tool list, the
        same shape `_on_catalog_grow` already logs.

        H11 Part B: switching INTO cc: mid-session (provider WASN'T "cc",
        now is) stashes the prior log as one `<conversation-so-far>` user
        message for the next fresh claude subprocess start to prime with
        (documented v1 behaviour -- see agent/cc_runtime.py's
        `prepare_conversation_so_far`). Switching AWAY from cc: needs no
        special handling at all: every cc: turn already logged ordinary
        user/assistant/tool_result nodes, so `derive_request` picks them
        up as history exactly like any other route's own turns.

        H11b finding 10: a cc:->cc: MODEL change (e.g. `/model cc:opus`
        then `/model cc:sonnet`) and a cc:->other switch both close the
        live claude subprocess (it can't hot-swap `--model` mid-stream) --
        `_cc_state` is dropped but the log's own `cc_session_id` meta node
        is left alone, so the NEXT `cc:` turn's `ensure_cc_state`
        naturally restarts with `--resume <same id> --model <new model>`,
        continuing the SAME claude conversation under the new model. A
        cc:->or:->cc: round trip (the live process was never closed, just
        idle) reuses that same still-alive process, so `prepare_
        conversation_so_far` alone would leave its own `<conversation-so-
        far>` stashed but unsent forever -- `ensure_cc_state`'s reuse path
        now drains it (capped) before returning, see that function."""
        old_model_raw = self.model_ref.raw
        self._fire_model_switch("PreModelSwitch", old_model=old_model_raw, new_model=model_ref.raw)
        if model_ref.provider == "cc" and self.model_ref.provider != "cc":
            from halo_harness.agent import cc_runtime
            cc_runtime.prepare_conversation_so_far(self)
        elif self.model_ref.provider == "cc" and (
            model_ref.provider != "cc" or model_ref.model != self.model_ref.model
        ):
            from halo_harness.agent import cc_runtime
            cc_runtime.close_cc(self)
            self._cc_state = None
        self.model_ref = model_ref
        self.model_profile = model_profile
        if creds is not None:
            self.creds = creds
        self.model_label = model_ref.raw
        self.route = Route(provider=model_ref.provider, upstream_model=model_ref.model, dialect=model_ref.dialect)
        # item 22 remainder: same state_dir threading as __init__ above --
        # a /model switch onto a Databricks endpoint with its own learned
        # rule picks it up immediately, not just a freshly-started session.
        self.provider_profile = resolve_profile(self.route, state_dir=self.state_dir)
        # 1.0.1 fixpass finding 6: `self.cost_meter` was built ONCE, at
        # Session construction time, from the STARTING model's own prices
        # (see __init__ above) and never touched again here -- every turn
        # after a `/model` switch kept billing (and the status bar kept
        # showing) the OLD model's per-token rates against the NEW model's
        # real usage. `total_usd`/`turns` accumulated so far are correctly
        # left alone (spend already recorded is real spend, priced at
        # whatever was true when it happened) -- only the METER's rates
        # move to match what NEW usage will actually cost from here on.
        self.cost_meter.price_in = model_profile.price_in
        self.cost_meter.price_out = model_profile.price_out
        self.cost_meter.price_cache_read = model_profile.price_cache_read
        self.cost_meter.price_cache_write = model_profile.price_cache_write

        # 1.0.1 hotfix 20.4: `/model` re-clamps the CARRIED-OVER effort for
        # the NEW route -- an effort value valid on the old model (say
        # `xhigh` on a DeepSeek chat route) can be exactly the value the
        # NEW route's own `output_config.effort` schema rejects (item 19's
        # bug, but triggered by a model switch instead of session start).
        # `effort_change_note` is read by both callers of this method
        # (agent/loop.py's own command pump and controller.py's `set_model`)
        # to tell the user their effort level just changed under them.
        self.effort_change_note: Optional[str] = None
        from halo_harness.providers.profiles import clamp_effort
        from halo_harness.providers.request import _anthropic_model_supports_adaptive_thinking
        if self.effort is not None:
            clamped = clamp_effort(self.effort, self.provider_profile)
            if clamped != self.effort:
                self.effort_change_note = f"Effort level adjusted to '{clamped}' for {model_ref.raw} (was '{self.effort}')"
                # Halo 2.0.1 W2a: the value carried over from the OLD route
                # (itself already "sent" there) is what's being reinterpreted
                # as a request on the NEW one -- record it as "requested" so
                # `requested_vs_sent` can show the switch-triggered change,
                # not just a same-value no-op.
                self.effort_requested = self.effort
                self.effort = clamped
        elif (self.provider_profile.thinking_format == "anthropic_thinking"
              and _anthropic_model_supports_adaptive_thinking(self.model_ref.model)):
            # Switching INTO an Anthropic-family route with no effort set at
            # all yet (e.g. this session started on a chat-dialect model)
            # gets the same "high" default a session starting there would --
            # 1.0.1 fixpass finding 11: only when the NEW model is adaptive-
            # capable (see __init__'s matching comment); a non-adaptive
            # model keeps "omit -> provider default" instead.
            self.effort = "high"
            self.effort_source = "default"
        elif self.provider_profile.default_effort_when_unset and self.provider_profile.reasoning_default_effort:
            # Halo 2.0.1: switching INTO Databricks GLM with no effort set at
            # all gets the route default (`high`) explicitly, never the
            # gateway's own `max` (see __init__'s matching comment).
            self.effort = self.provider_profile.reasoning_default_effort
            self.effort_source = "default"

        for name in self.tool_registry.names():
            tool = self.tool_registry.get(name)
            if hasattr(tool, "vision"):
                tool.vision = model_profile.vision
        if self.session_catalog is not None:
            from halo_harness.agent.catalog import host_cap
            self.session_catalog.cap = host_cap(model_ref.provider)
            self.session_catalog.vision = model_profile.vision
            while (len(self.session_catalog.names) > self.session_catalog.cap
                   and self.session_catalog._evict_one()):
                pass
            self.log.append_meta(model=model_ref.raw,
                                  tools=self.tool_registry.definitions_for(self.session_catalog.names))
        else:
            self.log.append_meta(model=model_ref.raw, tools=self.tool_registry.definitions())
        self._fire_model_switch("PostModelSwitch", old_model=old_model_raw, new_model=model_ref.raw)

    def _fire_model_switch(self, event: str, *, old_model: str, new_model: str) -> None:
        """W4a: PreModelSwitch/PostModelSwitch -- "`/model`" (removed from
        NOT_EMITTED_V1). `PostModelSwitch` is one of hooks.py's own
        `_CONTEXT_ONLY_EVENTS` -- a plain-stdout reply becomes
        `additionalContext`, logged as a snapshot exactly like SessionStart's
        own already does, so a hook can brief the model on what changed."""
        if self.hook_runner is None or not self.hook_runner.has_hooks(event):
            return
        payload = self.hook_runner.payload(event, extra={"old_model": old_model, "new_model": new_model})
        try:
            outcome = self._run_hook(event, payload, matched=new_model)
        except Exception:
            return
        if outcome.additional_context:
            self.log.append_snapshot([{"type": "text", "text": outcome.additional_context}], kind="hook_context")

    def status_event(self, *, phase: str = "idle", context_tokens=None, turn: Optional[int] = None) -> events.Event:
        """The D-Contract `status` payload as THIS session knows it --
        permission_mode/session_id/context_limit filled from live state, so
        a UI never has to guess them. `mcp` comes from `self.mcp_status_fn`
        when the caller installed one (the Controller does; a bare Session
        reports 0/0).

        1.0.1 hotfix 14: `context_tokens` defaults to `self._last_prompt_
        tokens` (0 before the first reply of the session/since the last
        `/clear`) rather than None when the caller doesn't pass one
        explicitly -- session-start/`/clear`/mode-change/model-switch all
        call this with no `context_tokens=` override, and the status bar
        needs a real number (0, not "unknown") to show "ctx 0/1M 0%"
        before the first turn rather than blanking the field."""
        mcp = self.mcp_status_fn() if self.mcp_status_fn else {"connected": 0, "total": 0}
        # 1.0.1 part 2 (item 22 remainder): the status bar's own effort tag
        # shows "<value> (tools)" whenever this route forces an explicit
        # reasoning_effort override alongside tools (the gpt-6 table rule,
        # or a learned per-endpoint rule) -- what's ACTUALLY sent on
        # essentially every real turn, never the raw configured value that
        # would otherwise read as a lie the moment the next turn goes out.
        from halo_harness.providers.profiles import effort_display_override
        effort_tag = effort_display_override(self.provider_profile) or self.effort
        return events.status(
            phase=phase, model=self.model_ref.raw, turn=self.turn_count if turn is None else turn,
            context_tokens=context_tokens if context_tokens is not None else (self._last_prompt_tokens or 0),
            context_limit=self.model_profile.context_tokens,
            cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None,
            permission_mode=self.permission_engine.mode, session_id=self.log.session_id, mcp=mcp,
            total_input_tokens=self.cost_meter.total_input_tokens,
            total_output_tokens=self.cost_meter.total_output_tokens,
            effort=effort_tag,
        )

    @property
    def busy(self) -> bool:
        """scope 0(c): True while a turn is actively running -- Controller.
        submit reads this (a plain Event check, thread-safe) to decide
        whether new input becomes a fresh turn or a steer."""
        return self._busy.is_set()

    def steer(self, text: str) -> bool:
        """Thread-safe, called DIRECTLY (never through the command queue,
        same reasoning as `interrupt`/`resolve_permission`: the worker is
        parked inside the running turn's generator and can't read a
        queue). Just queues `text` -- the actual cut/apply only ever
        happens from INSIDE the turn (`_step`'s stream loop,
        `_dispatch_tools`'s per-call loop, `_turn_body`'s post-dispatch/
        post-stream safe points), never from here. Returns False (nothing
        queued) when no turn is running to steer.

        finding 5: the busy CHECK and the enqueue are one atomic operation
        under `_steer_lock` -- the old code checked `self.busy` OUTSIDE the
        lock, so a turn could finish (`turn()`'s `finally` clearing `_busy`)
        in the window between that check and actually appending to
        `_steer_queue`, silently queuing text for a turn that had already
        ended (never drained by anything, since the old `finally` never
        looked). `turn()`'s own `finally` now clears `_busy` under this
        SAME lock, so the two are fully serialized: either this call's
        enqueue is visible to that drain (and `run()` resubmits it), or
        this call runs after the drain and correctly sees `_busy` already
        cleared, returning False -- callers (`Controller.submit`,
        headless.py's stream-json reader) must treat False as "start a new
        turn instead" rather than silently dropping the text."""
        if not text:
            return False
        if self.model_ref.provider == "cc":
            # H11 Part B: a cc: steer is sent to the running claude
            # subprocess IMMEDIATELY (Claude Code queues it internally --
            # its own result JSON's queued_turn_count confirms this)
            # rather than queued here for a `_step`/`_dispatch_tools`
            # safe point a cc: turn never runs.
            from halo_harness.agent import cc_runtime
            return cc_runtime.steer_cc(self, text)
        with self._steer_lock:
            if not self._busy.is_set():
                return False
            self._steer_queue.append(text)
        return True

    def queue_log_write(self, kind: str, payload: dict) -> bool:
        """H5c finding 14: thread-safe way for a caller OUTSIDE the
        session's own worker thread (the UI thread's `@file`-mention
        ingestion, a prompt-kind slash command's own `@path` snapshot, or
        a Textual worker thread's inline `!cmd`) to write to `self.log`
        WITHOUT racing the worker thread's own writes while a turn is
        running -- the exact bug verified with `ant:`: a steer carrying
        `@b.txt`, typed while a Read tool is running, made the NEXT
        request's user message `['text', 'tool_result', 'text']` (text
        before tool_result, which Anthropic 400s on), and a `!cmd` landing
        between an assistant `tool_use` and its own `tool_result` broke
        pairing on every route.

        Applied immediately (synchronously, on the CALLING thread) when
        idle -- correct and safe, since nothing else can be touching the
        log at the same time; this is also the ordinary case (nothing
        running to race with). While BUSY, queued instead (the SAME
        `_steer_lock` `steer()` uses) and applied later by
        `_apply_pending_steers_events`, at the SAME safe point a steer's
        own text is -- always AFTER whatever tool_result is already
        logged for a call that was allowed to finish (that method is only
        ever invoked from such a safe point), and ahead of the steer text
        itself, so `[..., tool_result, mention-or-!cmd, steer text]` stays
        correctly ordered.

        `kind`/`payload` mirror `_apply_log_write`'s own dispatch: `kind`
        is `"snapshot"` (`payload = {"blocks": [...], "snapshot_kind":
        str}`) or `"inline_shell"` (`payload = {"tool_use_id", "command",
        "content", "is_error"}`). Returns True iff queued (the caller may
        want to tell the user it will show up once the turn reaches a
        safe point), False iff applied immediately."""
        with self._steer_lock:
            if self._busy.is_set():
                self._pending_worker_writes.append((kind, payload))
                return True
        self._apply_log_write(kind, payload)
        return False

    def _apply_log_write(self, kind: str, payload: dict) -> None:
        if kind == "snapshot":
            self.log.append_snapshot(payload["blocks"], kind=payload.get("snapshot_kind", "at_mention"))
        elif kind == "inline_shell":
            self.log.append_assistant(content=[
                {"type": "tool_use", "id": payload["tool_use_id"], "name": "Bash",
                 "input": {"command": payload["command"]}},
            ])
            self.log.append_tool_result(tool_use_id=payload["tool_use_id"], content=payload["content"],
                                         is_error=payload["is_error"])

    def _pending_steer(self) -> bool:
        with self._steer_lock:
            return bool(self._steer_queue)

    def _pop_all_steers(self) -> list:
        with self._steer_lock:
            texts, self._steer_queue = self._steer_queue, []
        return texts

    def _apply_pending_steers_events(self, turn_no: int):
        """Pop every queued steer (in arrival order) and apply each as a
        user-role message -- called ONLY from a safe point (never mid-
        stream/mid-dispatch): right after a model call was cut short for a
        steer, or right after the current tool dispatch finished ("tools
        allowed to finish", scope 0c). Returns True iff at least one STEER
        was applied, via the generator's return value (`yield from`) -- the
        caller must then `continue` its loop instead of ending the turn
        (a pending WRITE with no accompanying steer text -- an inline
        `!cmd` run mid-turn with no redirect message -- must never force
        that; the ongoing turn continues undisturbed, and the next model
        call simply sees the new log content via the usual full re-derive).

        H5c finding 14: pending `@mention`/`@path`/inline-`!cmd` log
        writes (`queue_log_write`, queued while busy from OUTSIDE this
        session's own worker thread) are applied FIRST, at this exact safe
        point -- see that method's own docstring for why this ordering is
        what keeps tool_result-before-text correct."""
        with self._steer_lock:
            writes, self._pending_worker_writes = self._pending_worker_writes, []
        for kind, payload in writes:
            self._apply_log_write(kind, payload)
        texts = self._pop_all_steers()
        applied_any = False
        for i, text in enumerate(texts):
            # H5c finding 19: for an INTERACTIVE (Controller-driven) caller,
            # this is a SECOND `steer_queued` -- `Controller.submit` already
            # pushed one straight to the UI the instant the user submitted
            # (deliberately, for instant feedback -- see its own docstring),
            # so the TUI's own dispatch handler is what deduplicates the
            # resulting note (see `tui/dispatch.py`'s `_steering_notes_
            # pending` counter) rather than this method dropping the event
            # outright -- a bare `Session`/print-mode caller (headless.py's
            # stream-json reader, every test that drives `session.turn()`
            # directly with no Controller at all) has NO other path to ever
            # see `steer_queued`, and print mode's own documented contract
            # is "the same via --input-format stream-json ... Events:
            # steer_queued, steer_applied" -- removing this here would
            # silently drop it for both.
            yield events.steer_queued(text, turn=turn_no)
            # H5c finding 23: a steer becomes a user-role message exactly
            # like the FIRST message of a turn does (see `turn()`'s own
            # UserPromptSubmit handling above) -- it must run through the
            # SAME gate. Before this fix, typing while a turn ran was a
            # standing bypass of a user's own UserPromptSubmit hook (a
            # prompt filter/annotator that only ever fired for the first
            # message of a turn). `steer_applied` is still yielded on the
            # blocked branch (never `continue`d past) -- it's the TUI's
            # only signal that this queued steer finished processing (see
            # the dedup note above); only the LOG WRITE and `user_message`
            # are skipped.
            blocked_reason = None
            if self.hook_runner is not None and self.hook_runner.has_hooks("UserPromptSubmit"):
                payload = self.hook_runner.payload("UserPromptSubmit", prompt_id=f"prompt_{turn_no}_steer_{i}",
                                                     extra={"prompt": text})
                outcome = self._run_hook("UserPromptSubmit", payload, matched="", abort=self.abort)
                for msg in outcome.system_messages:
                    yield events.notification(msg)
                if outcome.blocked:
                    blocked_reason = outcome.block_reason or "blocked by a UserPromptSubmit hook"
                elif outcome.additional_context:
                    self.log.append_snapshot([{"type": "text", "text": outcome.additional_context}], kind="hook_context")
            if blocked_reason is not None:
                yield events.notification(f"Steer dropped: {blocked_reason}", level="error")
                yield events.steer_applied(text, turn=turn_no)
                continue
            self.log.append_user([{"type": "text", "text": text}], kind="steer")
            yield events.user_message(text, turn=turn_no)
            yield events.steer_applied(text, turn=turn_no)
            applied_any = True
        return applied_any

    def _pump_turn(self, text: str, images, out) -> None:
        """Drive ONE turn to completion, pushing every Event straight to
        `out` (a queue's `.put`) as it arrives. The abort Event is cleared
        here, at the start of the turn, so an Esc that arrived while the
        session was idle never kills the NEXT prompt."""
        self.abort.clear()
        try:
            for event in self.turn(text, images=images):
                out(event)
        except BaseException as e:  # never let a worker thread die silently, the UI would just hang
            log.exception("turn failed")
            out(events.error(f"{type(e).__name__}: {e}", turn=self.turn_count))
            self._fire_stop_failure(self.turn_count, f"{type(e).__name__}: {e}")
            out(events.turn_done(turn=self.turn_count, reason="error"))
            return

    def _pump_compaction(self, instructions: Optional[str], out) -> None:
        """finding 11 (major, h4-h5-h3c review) / U5 must-do: drives a
        MANUAL `/compact` on the WORKER thread, same as `_pump_turn` does
        for an ordinary turn -- the old code ran `session._run_compaction`
        to completion synchronously ON THE TEXTUAL EVENT LOOP itself (via
        `Controller.run_slash`, documented as "UI thread, synchronous,
        cheap"), freezing the whole UI (no repaint, no Esc) for the entire
        summarisation call. `_run_compaction`'s own `compaction` events
        stream through `out` exactly like any turn event, so the TUI's new
        `compaction` handler (tui/dispatch.py) can render a live
        "Compacting..." indicator instead of the screen just hanging.

        H9 whole-tree review finding 17: `_busy` set/cleared around this
        exactly like `turn()` sets it around itself -- a manual `/compact`
        never set it at all before, so a live `@mention`/`!cmd` typed on
        the UI thread while this ran took `queue_log_write`'s "not busy"
        branch and wrote straight into `self.log`, racing this method's own
        concurrent compaction writes on the worker thread (verified: an
        `@notes.txt` snapshot landed in the log BEFORE the `compacted`
        marker, invisible to the model for that exact call)."""
        self.abort.clear()
        self._busy.set()
        try:
            for event in self._run_compaction(self.turn_count, trigger="manual", custom_instructions=instructions):
                out(event)
        except BaseException as e:  # never let a worker thread die silently, the UI would just hang
            log.exception("manual /compact failed")
            out(events.compaction(phase="failed", trigger="manual", reason=f"{type(e).__name__}: {e}"))
        finally:
            self._end_busy_period()

    def _pump_clear(self, out) -> None:
        """U5 must-do: `/clear` runs on the worker thread too, same
        reasoning as `_pump_compaction` -- `SessionStart`/`SessionEnd`
        hooks are arbitrary user scripts and must never block the UI
        thread. Emits a `status` (fresh idle state, new session_id) so the
        TUI immediately reflects the new log.

        H9 whole-tree review finding 17: `_busy` set/cleared around this
        too -- same race, same fix, see `_pump_compaction`'s own comment."""
        self.abort.clear()
        self._busy.set()
        try:
            self.clear()
            out(self.status_event(phase="idle"))
        except BaseException as e:  # never let a worker thread die silently, the UI would just hang
            log.exception("/clear failed")
            out(events.notification(f"/clear failed: {type(e).__name__}: {e}", level="error"))
        finally:
            self._end_busy_period()

    def run(self, commands, out, *, mcp_status_fn=None) -> int:
        """The worker-thread command pump (D-Contract): consume `Command`s
        from `commands` (a `queue.Queue`) until a `None` sentinel arrives,
        pushing every `Event` the session produces to `out` (a callable --
        the Controller passes its event queue's `.put`). Returns when the
        session is done.

        `interrupt` and the reply kinds are ALSO reachable through the UI
        thread calling `abort.set()`/`resolve_permission()`/
        `resolve_question()` directly -- that is the ONLY way they can
        affect a turn already in flight, since this loop is parked inside
        that turn's generator while it runs (and inside the reply wait)."""
        self.mcp_status_fn = mcp_status_fn
        self._event_sink = out
        out(self.status_event(phase="idle"))
        while True:
            cmd = commands.get()
            if cmd is None:
                return 0
            if self._stopping.is_set():
                # review finding 2: `Controller.quit()` sets this (an
                # atomic flag, safe to check here) BEFORE queuing its own
                # `None` sentinel -- a `user_input` a user typed just
                # before quitting can otherwise sit AHEAD of that sentinel
                # in the queue and `_pump_turn`'s own `self.abort.clear()`
                # would silently undo the abort quit() just set, starting
                # a whole new turn (and its own child processes) during
                # shutdown. Once stopping, every queued command except the
                # sentinel itself is drained and ignored.
                continue
            kind = getattr(cmd, "kind", None)
            data = getattr(cmd, "data", None) or {}
            if kind == "user_input":
                self._pump_turn(data.get("text", ""), data.get("images"), out)
                # finding 5: whatever `turn()`'s own `finally` drained as
                # "never got applied" is resubmitted here, on THIS same
                # worker thread, as the next `user_input` Command -- run()
                # picks it right back up on the NEXT loop iteration (a
                # fresh turn, since `_busy` is already clear by now), so a
                # steer that missed every internal checkpoint still isn't
                # silently lost, it just becomes the next turn instead.
                if self._leftover_steer_texts:
                    leftover, self._leftover_steer_texts = self._leftover_steer_texts, []
                    for leftover_text in leftover:
                        commands.put(events.Command("user_input", {"text": leftover_text}))
            elif kind == "run_clear":
                self._pump_clear(out)
            elif kind == "run_compact":
                # finding 11 / U5: handled BETWEEN turns, same slot in this
                # loop as user_input -- Controller.run_compact (below) is
                # the only enqueuer and already refuses to queue this while
                # `self.busy` is True, so this branch never actually runs
                # concurrently with a turn; it's still safe either way
                # (this loop only ever does one Command at a time).
                self._pump_compaction(data.get("instructions"), out)
            elif kind == "interrupt":
                self.abort.set()
            elif kind == "steer":
                # safety net only, mirrors "permission_reply"/"question_
                # reply" -- the UI/print-mode caller normally calls
                # Session.steer() directly (unblocks the running turn
                # immediately); this branch only ever fires for a "steer"
                # Command that arrived while NO turn was running, where
                # there is nothing to steer.
                self.steer(data.get("text", ""))
            elif kind == "set_mode":
                mode = data.get("mode")
                if mode:
                    self.permission_engine.mode = mode
                out(self.status_event())
            elif kind == "set_model":
                self.set_model(data["model_ref"], data["model_profile"], data.get("creds"))
                if self.effort_change_note:
                    out(events.notification(self.effort_change_note, level="info"))
                out(self.status_event())
            elif kind == "permission_reply":
                # safety net only: the UI answers a pending request by
                # calling resolve_permission() directly (this branch is
                # unreachable while that wait is in flight).
                self.resolve_permission(data.get("id"), data.get("decision"))
            elif kind == "question_reply":
                self.resolve_question(data.get("id"), data.get("answer"))
            elif kind == "plan_reply":
                # safety net only, mirrors "permission_reply"/"question_
                # reply" just above -- Controller.answer_plan normally
                # calls resolve_plan() directly (this branch is unreachable
                # while that wait is in flight).
                self.resolve_plan(data)

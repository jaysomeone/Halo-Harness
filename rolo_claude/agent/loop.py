"""rolo_claude.agent.loop -- the agent loop (H1 rewrite, scope E-H; H2
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

import dataclasses
import json
import logging
import os
import threading
import time
import uuid
from typing import Iterator, Optional

from rolo_claude import events
from rolo_claude.agent.compact import (
    build_files_read_snapshot, build_summary_instruction, resolve_knobs, select_verbatim_tail,
    should_compact, tail_retention_tokens, validate_summary, wrap_compacted_summary,
)
from rolo_claude.agent.compact import opencode_usable as _opencode_usable
from rolo_claude.agent.derive import content_hash_from_oai_body, derive_request
from rolo_claude.agent.invariants import repair_truncated_text, synthesize_missing_results, validate_tool_use
from rolo_claude.agent.log import SessionLog
from rolo_claude.agent.repair import repair_assistant_turn
from rolo_claude.hooks import HookRunner, build_prompt_caller, load_plugin_hooks, merge_hook_maps, normalize_hooks
from rolo_claude.model import CostMeter, ModelProfile, ModelRef
from rolo_claude.permissions import Decision, PermissionEngine
from rolo_claude.providers.errors import (
    CONTEXT_WINDOW_EXCEEDED, MAX_RETRIES, is_reasoning_replay_bug, is_retryable_message, retry_delay_ms,
)
from rolo_claude.providers.hooks import (
    classify_length_tool_call, is_retryable_empty_completion, leak_parser,
    max_tokens_budget, overflow_classifier, record_databricks_output_tokens,
)
from rolo_claude.providers.config import tool_child_env
from rolo_claude.providers.profiles import ProviderProfile, resolve_profile
from rolo_claude.providers.request import ToolCatalogTooLarge, build_anthropic_request_body, build_request_body
from rolo_claude.providers.routing import Route
from rolo_claude.providers.stream import (
    CompletionRequest, ContextOverflow, ProviderCreds, ProviderNotConfigured,
    UpstreamError, stream_anthropic_completion, stream_completion,
)
from rolo_claude.tools.base import ToolContext, ToolResult
from rolo_claude.tools.registry import ToolRegistry, run_read_only_batch
from rolo_claude.tools.truncate import spill_and_truncate

log = logging.getLogger("bridge")

_MAX_RETRY_WAIT_S = 60.0  # per-wait cap, never the uncapped `time.sleep(Retry-After)` H0 had

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
    from rolo_claude.tools.mcp_tool import cap_and_spill
    filtered = [b for b in blocks if not (isinstance(b, dict) and b.get("type") == "tool_reference")]
    if not filtered:
        filtered = [{"type": "text", "text": "(no content returned)"}]
    return cap_and_spill(filtered, meta=meta, session_dir=session_dir, tool_use_id=tool_use_id)


def _summary_text_for_blocks(blocks: list) -> str:
    """A short, human-readable stand-in for the `tool_result` EVENT's
    `summary` field (display/verbose-log only -- never what's logged to
    the session or sent to the model, which is the full block list) --
    the first real text block, or an honest placeholder naming whatever
    non-text content came back (e.g. "[image]")."""
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
            return b["text"]
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


class _StepResult:
    def __init__(self, *, assistant_blocks, stop_reason, usage, reasoning, body, tool_call_flags=None):
        self.assistant_blocks = assistant_blocks
        self.stop_reason = stop_reason
        self.usage = usage
        self.reasoning = reasoning
        self.body = body  # the exact prebuilt_oai_body this step sent (finding 4's hash source)
        self.tool_call_flags = tool_call_flags or {}  # tool_use id -> {truncated_by_length|malformed_json,...}


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
        permission_engine: Optional[PermissionEngine] = None,
        session_catalog: Optional[object] = None, mcp_manager: Optional[object] = None,
        hook_runner: Optional[HookRunner] = None,
    ):
        self.cwd = cwd
        self.model_ref = model_ref
        self.small_model_ref = small_model_ref
        self.model_profile = model_profile
        self.creds = creds
        self.state_dir = state_dir
        self.model_label = model_label
        self.effort = effort
        self.turn_count = 0
        self.max_turns = max_turns
        # H5 scope D: per-family fallback pricing (model.CostMeter._fallback_cost)
        # for whenever a response has no usage.cost of its own.
        self.cost_meter = CostMeter(price_in=model_profile.price_in, price_out=model_profile.price_out)
        self.tool_registry: ToolRegistry = session_context.tool_registry
        self.route = Route(provider=model_ref.provider, upstream_model=model_ref.model, dialect=model_ref.dialect)
        self.provider_profile: ProviderProfile = resolve_profile(self.route)
        self.openrouter_base_url = openrouter_base_url
        self.extra_headers = extra_headers or {}
        self._loop_breaker: dict = {}  # canonical (name,args) -> consecutive count, reset every turn
        # must-do 5: an interrupt source (Bash kill/Esc -- none exists yet
        # in `-p`) sets this; `_step` passes it into `stream_completion`
        # (cuts phase 2 short) and observes it in every retry wait.
        self.abort = threading.Event()
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
        # H5 scope B: kept for re-injection after compaction
        # (claude_md_text()/memory_snapshot_text()) -- session_context was
        # a constructor-only local before this milestone.
        self.session_context = session_context
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
        # print-mode `ask` denials accumulate here for the json result's
        # `permission_denials` (rolo_claude/output.py reads this list).
        self.permission_denials: list = []
        # U2/D-Contract: `interactive=True` (set by the TUI's Controller, and
        # ONLY by it) makes an `ask` decision REALLY block -- the turn emits
        # `permission_request` and then waits on a threading.Event keyed by
        # the tool_use id until the UI calls `resolve_permission`. H2b's
        # non-blocking "unavailable in this session" denial stays the
        # behaviour for `-p` and for a bare Session in a unit test.
        self.interactive = False
        self._permission_waiters: dict = {}
        # U2: the AskUserQuestion round trip parks here, keyed by tool_use
        # id, exactly like `_permission_waiters`.
        self._question_waiters: dict = {}
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
        # D-CFG: `stop_hook_active` is True only on a RE-ENTRANT Stop-hook
        # evaluation within the SAME "the model tried to stop" attempt --
        # reset at the start of every new turn() call, never carried from
        # a previous turn.
        self._stop_hook_active = False

        self.log = session_log or SessionLog(cwd)
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
            self._fire_session_start("resume")

    def _fire_session_start(self, source: str) -> None:
        """H4 scope B: SessionStart(startup|resume|clear|compact) -- `clear`/
        `compact` are forward-wired call sites for whichever future
        milestone actually implements `/clear`/compaction; only `startup`/
        `resume` fire today. Hook-added `additionalContext` becomes a
        user-role SNAPSHOT in the log (never the system node -- brief B)."""
        if self.hook_runner is None or not self.hook_runner.has_hooks("SessionStart"):
            return
        payload = self.hook_runner.payload("SessionStart", extra={"source": source})
        outcome = self.hook_runner.run("SessionStart", payload, matched=source)
        if outcome.additional_context:
            self.log.append_snapshot([{"type": "text", "text": outcome.additional_context}], kind="hook_context")
        if source in ("startup", "resume"):
            from rolo_claude.hooks import env_file_path, read_env_file_exports
            exports = read_env_file_exports(env_file_path(self.log.session_id))
            if exports:
                self.tool_env = {**self.tool_env, **exports}

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

    def _call_model_for_hook(self, prompt_text: str, timeout_s: float) -> str:
        """The `prompt`/`agent` hook handler types' "small model via the
        provider layer" call [D-CFG] -- a ONE-SHOT request built directly
        via `build_request_body` (never through `derive_request`/the
        session log, so a hook's own model call is NEVER part of the
        logged conversation). Uses `self.small_model_ref` when configured,
        else falls back to the main model -- true cross-provider small-
        model routing (separate creds/profile resolution) is a follow-up
        refinement; this already gives every hook a real model call today."""
        ref = self.small_model_ref or self.model_ref
        route = Route(provider=ref.provider, upstream_model=ref.model, dialect=ref.dialect) \
            if ref is not self.model_ref else self.route
        profile = resolve_profile(route) if ref is not self.model_ref else self.provider_profile
        body = build_request_body(
            system_text="Reply with ONLY a single JSON object {\"ok\": true|false, \"reason\": \"...\"} -- no prose.",
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt_text}]}],
            tools=[], route=route, profile=profile, effort=self.effort,
            context_tokens=self.model_profile.context_tokens,
            prompt_estimate=_rough_estimate("", []),
            requested_max_tokens=min(1024, self.model_profile.max_output_tokens or 1024),
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
                    raise RuntimeError((ev.get("error") or {}).get("message", "hook model call failed"))
        finally:
            timer.cancel()
            gen.close()
        return "".join(text_parts)

    def _on_catalog_grow(self, names: list) -> None:
        """H3 scope C: `SessionCatalog.load()`'s own callback -- a
        ToolSearch call just appended one or more deferred MCP tools to
        the session's wire catalog (or LRU-evicted one back out). Logs a
        NEW `meta` node carrying the full, still name-order-frozen-then-
        append-only tool list (`derive_request` always takes the LAST
        meta node's `tools`, so this is the only write needed for the
        NEXT request to carry it -- see agent/derive.py)."""
        self.log.append_meta(tools=self.tool_registry.definitions_for(names))

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
        kwargs = dict(
            body={"messages": []}, route=self.route,
            profile={"context_tokens": self.model_profile.context_tokens,
                     "max_output_tokens": self.model_profile.max_output_tokens},
            creds=self.creds, state_dir=self.state_dir, extra_headers=self.extra_headers,
            model_label=self.model_ref.raw, openrouter_base_url=self.openrouter_base_url,
            harness_mode=True, ping_interval=float(os.environ.get("BRIDGE_PING_INTERVAL", "15")),
            tool_id_format=self.provider_profile.tool_id_format,
        )
        if self.route.dialect == "anthropic-passthrough":
            kwargs["prebuilt_anthropic_body"] = body
        else:
            kwargs["prebuilt_oai_body"] = body
        return CompletionRequest(**kwargs)

    def _stream(self, req: CompletionRequest) -> Iterator[dict]:
        """Dispatch to the right dialect's orchestration -- the ONE place
        that decides `stream_completion` vs `stream_anthropic_completion`,
        so every `_step`/`_run_compaction` call site stays dialect-blind."""
        if self.route.dialect == "anthropic-passthrough":
            return stream_anthropic_completion(req, abort=self.abort)
        return stream_completion(req, abort=self.abort)

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
        while True:
            attempts += 1
            gen = self._stream(req)
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
                    if kind == "message_start":
                        yield events.message_start(turn=turn_no, model=self.model_ref.raw)
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
                            text = delta.get("text", "")
                            text = text if isinstance(text, str) else str(text)
                            assistant_blocks[idx]["text"] += text
                            yield events.thinking_delta(text, index=idx, turn=turn_no)
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
                return None

            if steered_cut:
                # scope 0(c): the stream was cut for a queued steer, not
                # an error -- any tool_use block that never reached its
                # own content_block_stop (input never parsed) is dropped;
                # a block that DID already close (rare, but the steer may
                # have been noticed on the very next chunk after one
                # finished) survives and is dispatched normally by
                # `_turn_body` -- "running tools allowed to finish" never
                # applied to it in the first place, since it hadn't
                # started. Falls straight through to a normal, un-retried
                # result (never the empty-completion-retry path below --
                # an intentional cut is not a provider failure).
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
                # H5 scope F item 2: OpenCode's message-pattern classifier
                # (Appendix B) is OR'd onto the existing status-table
                # decision -- either one saying "retry" is enough; only
                # BOTH saying "don't" stops the ladder. `retry_delay_ms`
                # (Appendix B's jittered 2s*2^n / retry-after-ms / retry-
                # after formula) drives the actual wait, still capped by
                # `_MAX_RETRY_WAIT_S` the same way the old ladder was.
                merged_retryable = e.retryable or is_retryable_message(
                    e.status, e.message, host=self.model_ref.provider,
                )
                if merged_retryable and attempts <= MAX_RETRIES:
                    hdrs = {"retry-after": e.retry_after} if e.retry_after else {}
                    delay = min(retry_delay_ms(attempts, hdrs) / 1000.0, _MAX_RETRY_WAIT_S)
                    if self._abort_sleep(delay):
                        return None
                    continue
                # must-do 3: overflow_classifier gives the CALLER a
                # dsh-style taxonomy bucket (AUTH/RATE_LIMIT/CONTEXT_WINDOW_
                # EXCEEDED/PROVIDER_FAILURE/...) alongside the raw
                # per-source wire err_type, so it can react by KIND of
                # failure without parsing vendor-specific strings itself.
                yield events.error(e.message, turn=turn_no, err_type=e.err_type, retryable=e.retryable,
                                    category=overflow_classifier(e.status, e.message))
                return None

            if wire_error is not None:
                message = wire_error.get("message", "unknown upstream error")
                if is_reasoning_replay_bug(message):
                    yield events.error(f"reasoning-replay bug (never retried): {message}", turn=turn_no, err_type="reasoning_replay_bug")
                    return None
                retryable = wire_error.get("type") in ("overloaded_error", "rate_limit_error", "api_error")
                merged_retryable = retryable or is_retryable_message(
                    wire_error.get("status"), message, host=self.model_ref.provider,
                )
                if merged_retryable and attempts <= MAX_RETRIES:
                    hdrs = {"retry-after": wire_error.get("retry_after")} if wire_error.get("retry_after") else {}
                    delay = min(retry_delay_ms(attempts, hdrs) / 1000.0, _MAX_RETRY_WAIT_S)
                    if self._abort_sleep(delay):
                        return None
                    continue
                # a wire error drops the partial reply -- never committed to the log
                wire_status = _WIRE_ERROR_TYPE_TO_STATUS.get(wire_error.get("type"), 500)
                yield events.error(message, turn=turn_no, err_type=wire_error.get("type", "error"),
                                    category=overflow_classifier(wire_status, message))
                return None

            # A clean stream from here on.
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
        cleaned = []
        for b in assistant_blocks:
            if b is None:
                continue
            if b.get("type") in ("text", "thinking") and isinstance(b.get("text"), str):
                b["text"] = repair_truncated_text(b["text"])
            cleaned.append(b)
        return _StepResult(assistant_blocks=cleaned, stop_reason=stop_reason, usage=usage, reasoning=reasoning,
                            body=body, tool_call_flags=harness_meta.get("tool_call_flags") or {})

    def _account_usage(self, result: "_StepResult") -> None:
        """Record cost/usage/OTPM bookkeeping for ONE model call's real
        `result.usage` -- called for EVERY `_step` call `_turn_body` makes,
        including one whose assistant content is later discarded (H2 scope
        B's tool_choice=required retry: a discarded attempt still spent
        real tokens against the account, so it must still count here even
        though it never becomes a logged assistant message)."""
        cost = self.cost_meter.add_usage(self.model_ref.provider, result.usage)
        self.log.append_usage(result.usage, cost)
        output_tokens = result.usage.get("output_tokens") if isinstance(result.usage, dict) else None
        if self.model_ref.provider == "databricks":
            record_databricks_output_tokens(self.model_ref.raw, output_tokens)
        # H5 scope B: the provider's own reported prompt size drives the
        # compaction trigger (`_maybe_auto_compact`) -- "the provider's last
        # prompt_tokens (else estimate)" per the brief.
        input_tokens = result.usage.get("input_tokens") if isinstance(result.usage, dict) else None
        if isinstance(input_tokens, int):
            self._last_prompt_tokens = input_tokens

    # ---- compaction (H5 scope B) -----------------------------------------

    def _build_body_for_messages(self, system_text: str, messages: list, tools, *,
                                  tool_choice=None, requested_max_tokens: Optional[int] = None) -> dict:
        """Build a wire body for an ARBITRARY (system_text, messages, tools)
        triple using this session's own route/profile/effort -- shared by
        `_derive_and_build` (derives straight from the log) and
        `_run_compaction` (derives from the log too, then appends one extra
        instruction message before building, so it cannot reuse
        `_derive_and_build` itself)."""
        if requested_max_tokens is None:
            requested_max_tokens = max_tokens_budget(self.provider_profile, model_key=self.model_ref.raw,
                                                       requested=None, abort=self.abort)
        if self.route.dialect == "anthropic-passthrough":
            return build_anthropic_request_body(
                system_text=system_text, messages=messages, tools=tools, route=self.route,
                profile=self.provider_profile, effort=self.effort,
                requested_max_tokens=requested_max_tokens, tool_choice=tool_choice,
            )
        return build_request_body(
            system_text=system_text, messages=messages, tools=tools, route=self.route,
            profile=self.provider_profile, effort=self.effort,
            context_tokens=self.model_profile.context_tokens,
            prompt_estimate=_rough_estimate(system_text, messages),
            requested_max_tokens=requested_max_tokens, tool_choice=tool_choice,
        )

    def _maybe_auto_compact(self, turn_no: int) -> Iterator[events.Event]:
        """Called right after `_account_usage` on every successful step:
        the 80%-of-headroom gate (agent/compact.py), using the provider's
        OWN last-reported `input_tokens` (else a rough estimate of the log
        as it stands right now)."""
        if self._last_prompt_tokens is not None:
            prompt_tokens = self._last_prompt_tokens
        else:
            system_text, messages, _ = derive_request(self.log, tools=None)
            prompt_tokens = _rough_estimate(system_text, messages)
        do_it, _trigger_tokens = should_compact(
            prompt_tokens, self.model_profile.context_tokens, self.model_profile.max_output_tokens,
            self._compaction_knobs,
        )
        if do_it:
            yield from self._run_compaction(turn_no, trigger="auto")

    def _run_compaction(self, turn_no: int, *, trigger: str, custom_instructions: Optional[str] = None) -> Iterator[events.Event]:
        """The dsh replay: one summarisation call whose prefix is the EXACT
        current derived request, plus one final user instruction demanding
        the 8-section checkpoint; validated with one corrective retry; new
        transcript = `<compacted-summary>` (merging any prior one, via the
        replay prefix -- see agent/derive.py) + a verbatim tail + re-injected
        snapshots + a files-read list, logged behind a `compacted` marker so
        `derive_request` skips everything before it. Never raises on a
        validation failure -- falls back to the best-effort text (a
        compaction that itself errors must never crash the turn it was
        trying to save room for)."""
        system_text, messages, tools = derive_request(self.log, tools=None)
        tokens_before = self._last_prompt_tokens or _rough_estimate(system_text, messages)
        yield events.compaction(phase="start", trigger=trigger, turn=turn_no, tokens_before=tokens_before)

        if self.hook_runner is not None and self.hook_runner.has_hooks("PreCompact"):
            payload = self.hook_runner.payload("PreCompact", extra={"trigger": trigger,
                                                                      "custom_instructions": custom_instructions})
            outcome = self.hook_runner.run("PreCompact", payload, matched=trigger)
            if outcome.additional_context:
                self.log.append_snapshot([{"type": "text", "text": outcome.additional_context}], kind="hook_context")

        instruction = build_summary_instruction(custom_instructions)
        summary_text, stop_reason, ok, missing = "", None, False, []
        for _attempt in range(2):  # one corrective retry (scope B)
            call_messages = messages + [{"role": "user", "content": [{"type": "text", "text": instruction}]}]
            body = self._build_body_for_messages(system_text, call_messages, tools,
                                                  requested_max_tokens=min(4096, self.model_profile.max_output_tokens or 4096))
            req = self._build_request(body)
            text_parts: list = []
            stop_reason = None
            gen = self._stream(req)
            try:
                for ev in gen:
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
                            self.log.append_usage(ev["usage"], cost)
                    elif kind == "error":
                        stop_reason = "error"
            except (ContextOverflow, UpstreamError, ProviderNotConfigured) as e:
                # OpenCode's own rule: the summarisation call itself can
                # overflow (a genuinely enormous history) or hit an
                # ordinary upstream failure -- never let that escape
                # uncaught (it would crash the very turn compaction was
                # trying to save room for). Treated as a validation
                # failure below, which drives the same corrective-retry-
                # then-best-effort-fallback path an empty/invalid reply
                # would; on total failure the TAIL + snapshots + marker
                # still get written (a real shrink even with no summary
                # text), giving `_turn_body`'s retry a real chance.
                log.warning("compaction summarisation call failed (%s: %s); treating as validation failure", type(e).__name__, e)
                stop_reason = "error"
            finally:
                gen.close()
            summary_text = "".join(text_parts)
            ok, missing = validate_summary(summary_text, stop_reason)
            if ok:
                break
            yield events.compaction(phase="retry", trigger=trigger, turn=turn_no, headings_missing=missing)
            instruction = build_summary_instruction(
                custom_instructions, corrective=True,
                problem=(f"missing sections: {', '.join(missing)}" if missing else "the reply was cut off"),
            )
        if not ok:
            log.warning("compaction summary failed validation twice (missing=%s); using it anyway", missing)

        wrapped = wrap_compacted_summary(summary_text or "(summary unavailable)")
        usable = _opencode_usable(self.model_profile.context_tokens, self.model_profile.max_output_tokens)
        tail = select_verbatim_tail(messages, tail_retention_tokens(usable))

        self.log.append_compacted(trigger=trigger, custom_instructions=custom_instructions)
        self.log.append_user([{"type": "text", "text": wrapped}])

        ctx_obj = self.session_context
        claude_md = ctx_obj.claude_md_text() if ctx_obj is not None else ""
        if claude_md:
            self.log.append_snapshot([{"type": "text", "text": claude_md}], kind="claude_md")
        memory_text = ctx_obj.memory_snapshot_text() if ctx_obj is not None else ""
        if memory_text:
            self.log.append_snapshot([{"type": "text", "text": memory_text}], kind="memory_index")
        files_text = build_files_read_snapshot(list(self._read_cache.keys()))
        if files_text:
            self.log.append_snapshot([{"type": "text", "text": files_text}], kind="files_read")

        for msg in tail:
            if msg.get("role") == "user":
                self.log.append_user(msg.get("content") or [])
            elif msg.get("role") == "assistant":
                self.log.append_assistant(content=msg.get("content") or [], reasoning=msg.get("reasoning"))

        if self.hook_runner is not None and self.hook_runner.has_hooks("PostCompact"):
            payload = self.hook_runner.payload("PostCompact", extra={"trigger": trigger, "compact_summary": summary_text})
            self.hook_runner.run("PostCompact", payload, matched=trigger)
        self._fire_session_start("compact")

        after_system, after_messages, _ = derive_request(self.log, tools=None)
        tokens_after = _rough_estimate(after_system, after_messages)
        self._last_prompt_tokens = None  # unknown until the next real reply's usage lands
        yield events.compaction(phase="done", trigger=trigger, turn=turn_no,
                                 tokens_before=tokens_before, tokens_after=tokens_after)

    # ---- the turn: model call(s) + tool dispatch ------------------------

    def turn(self, text: str, images: Optional[list] = None) -> Iterator[events.Event]:
        """Run one turn to completion (which may involve several model
        calls interleaved with tool dispatch, bounded by `--max-turns`
        MODEL CALLS -- finding 9, see `_turn_body`). Always ends by
        yielding a `turn_done` event."""
        self.turn_count += 1
        turn_no = self.turn_count
        self._loop_breaker = {}
        # H3/H4 must-do: a PREVIOUS turn's interrupt (Esc/Ctrl+C) leaves
        # `self.abort` set -- reused unchanged, a brand new turn would see
        # it already fired and abort immediately, before ever streaming a
        # single token. Each turn starts with a clean slate.
        self.abort.clear()
        self._stop_hook_active = False
        # H4 scope C: a Skill's `allowed-tools` only ever lasts "until the
        # next user message" -- this IS that next user message.
        self.permission_engine.clear_temporary_allow_rules()
        # scope 0(c): `busy` (and therefore whether Controller.submit
        # routes new input to a fresh turn or to `steer`) is True for the
        # WHOLE method body below, cleared no matter how it ends.
        self._busy.set()
        try:
            if self.hook_runner is not None and self.hook_runner.has_hooks("UserPromptSubmit"):
                payload = self.hook_runner.payload("UserPromptSubmit", prompt_id=f"prompt_{turn_no}",
                                                     extra={"prompt": text})
                outcome = self.hook_runner.run("UserPromptSubmit", payload, matched="")
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
            self._busy.clear()

    def _turn_body(self, turn_no: int) -> Iterator[events.Event]:
        # finding 9: `--max-turns` counts MODEL CALLS made WITHIN this one
        # turn (Claude Code semantics) -- `-p` calls `turn()` exactly once,
        # so counting turn() calls against it (the pre-H2 behavior) never
        # bounded anything inside a tool loop; only the identical-call
        # breaker (a SEPARATE guard, kept as-is) did.
        model_calls = 0
        while True:
            if model_calls >= self.max_turns:
                # H5 scope F item 9 (OpenCode Appendix H): inject
                # MAX_STEPS_PROMPT as a snapshot and give the model ONE more
                # (tool-less) call to close out with a real summary instead
                # of just silently ending mid-tool-loop.
                self.log.append_snapshot([{"type": "text", "text": MAX_STEPS_PROMPT}], kind="max_steps")
                final_result = yield from self._step(turn_no, tool_choice=None, no_tools=True)
                if final_result is not None and final_result is not _OVERFLOW_NEEDS_COMPACTION:
                    self._account_usage(final_result)
                    self.log.append_assistant(
                        content=final_result.assistant_blocks, stop_reason=final_result.stop_reason,
                        request_hash=content_hash_from_oai_body(final_result.body),
                    )
                    # Mirrors the ordinary per-step message_end (see below)
                    # so a print-mode JSON result reflects the WRAP-UP
                    # call's own stop_reason/usage, not a stale earlier one.
                    input_tokens = final_result.usage.get("input_tokens") if isinstance(final_result.usage, dict) else None
                    context_pct = (round(100.0 * input_tokens / self.model_profile.context_tokens, 1)
                                   if isinstance(input_tokens, int) and self.model_profile.context_tokens else None)
                    yield events.message_end(
                        turn=turn_no, stop_reason=final_result.stop_reason, usage=final_result.usage,
                        cost_usd=(self.cost_meter.total_usd if self.cost_meter.has_cost_data else None),
                        context_pct=context_pct,
                    )
                yield events.status(phase="idle", model=self.model_ref.raw, turn=turn_no,
                                     cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None)
                yield events.turn_done(turn=turn_no, reason="max_turns")
                return

            result = yield from self._step(turn_no)
            model_calls += 1
            if result is _OVERFLOW_NEEDS_COMPACTION:
                # H5 scope B: overflow -> compact -> retry ONCE.
                yield from self._run_compaction(turn_no, trigger="overflow")
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
                yield events.turn_done(turn=turn_no, reason="error")
                return
            self._account_usage(result)
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
            self.log.append_assistant(
                content=result.assistant_blocks, reasoning=result.reasoning,
                stop_reason=result.stop_reason, request_hash=req_hash,
            )
            input_tokens = result.usage.get("input_tokens") if isinstance(result.usage, dict) else None
            context_pct = None
            if isinstance(input_tokens, int) and self.model_profile.context_tokens:
                context_pct = round(100.0 * input_tokens / self.model_profile.context_tokens, 1)
            # finding 15: cost_usd is the SESSION's cumulative total (not
            # just this one call's own cost) -- output.py's JSON sink takes
            # message_end's cost_usd as-is (the latest value naturally IS
            # the running total by construction), so a three-call turn
            # reports its whole cost, not a third of it.
            yield events.message_end(
                turn=turn_no, stop_reason=result.stop_reason, usage=result.usage,
                cost_usd=(self.cost_meter.total_usd if self.cost_meter.has_cost_data else None),
                context_pct=context_pct,
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
                    stop_outcome = self.hook_runner.run_stop("Stop", last_assistant_message=last_text,
                                                              prompt_id=f"turn_{self.turn_count}")
                    for msg in stop_outcome.system_messages:
                        yield events.notification(msg)
                    if stop_outcome.blocked:
                        # D-CFG: stderr/reason becomes a user-role message
                        # and the loop continues.
                        continuation = stop_outcome.block_reason or "Please continue."
                        self.log.append_user([{"type": "text", "text": continuation}])
                        yield events.user_message(continuation, turn=turn_no)
                        continue
                yield events.status(phase="idle", model=self.model_ref.raw, turn=turn_no, context_tokens=input_tokens,
                                     context_limit=self.model_profile.context_tokens,
                                     cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None)
                # finding 3: a message's OWN max_tokens cutoff is not the
                # SESSION hitting --max-turns -- report it as what it is.
                reason = "max_tokens" if result.stop_reason == "max_tokens" else "end_turn"
                yield events.turn_done(turn=turn_no, reason=reason)
                return

            ended = yield from self._dispatch_tools(turn_no, tool_use_blocks, result.tool_call_flags)
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
                yield events.turn_done(turn=turn_no, reason="end_turn")
                return
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
            return item

        basic_error = validate_tool_use(tu)
        if basic_error is not None:
            item["text"] = basic_error
            return item

        if not outcome.ok:
            item["text"] = outcome.error_text
            item["repaired"] = outcome.repaired
            return item

        name = outcome.block.get("name")  # possibly renamed by repair
        tool_input = outcome.block.get("input") or {}
        # A block promoted from a text-embedded leak (H2 scope B) is
        # ALWAYS "repaired" for transparency -- it never arrived as a
        # native call, regardless of whether its name/schema also happened
        # to need fixing up.
        repaired = outcome.repaired or bool(tu.get("_promoted_from_leak"))
        item.update(name=name, input=tool_input, repaired=repaired)

        key = (name, _canonical_args(tool_input))
        count = self._loop_breaker.get(key, 0) + 1
        self._loop_breaker[key] = count
        item["count"] = count
        if count >= _LOOP_BREAKER_END_AT:
            item["text"] = f"Loop breaker: {name} called with the same arguments {count} times this turn -- ending the turn."
            item["end_turn"] = True
            return item
        if count >= _LOOP_BREAKER_DENY_AT:
            item["text"] = f"Loop breaker: {name} called with the same arguments {count} times -- denied. Try a different approach."
            return item

        if name == "AskUserQuestion" and self.interactive:
            # U2: the question IS the interaction -- it is never a permission
            # decision. `_dispatch_tools` emits `question` and parks this
            # call on the reply from `answer_question` instead of ever
            # reaching the tool's own run() (which stays print mode's error
            # path: agent/loop.py only takes this branch when a UI is
            # actually attached).
            item["pending_question"] = True
            self._question_waiters[tool_id] = {"event": threading.Event(), "answer": None}
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
            pre_outcome = self.hook_runner.run("PreToolUse", payload, matched=name, tool_name=name,
                                                 tool_input=tool_input, tool=tool, abort=self.abort)
            for msg in pre_outcome.system_messages:
                item.setdefault("hook_system_messages", []).append(msg)
            if pre_outcome.updated_input is not None:
                tool_input = pre_outcome.updated_input
                item["input"] = tool_input
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
                pr_outcome = self.hook_runner.run("PermissionRequest", pr_payload, matched=name, tool_name=name,
                                                    tool_input=tool_input, tool=tool, abort=self.abort)
                if pr_outcome.permission_decision in ("allow", "deny"):
                    reason = pr_outcome.permission_decision_reason or f"{pr_outcome.permission_decision}ed by a PermissionRequest hook"
                    decision = Decision(pr_outcome.permission_decision, reason, source="hook")
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
                if not self.interactive:
                    # no UI is attached (print mode / a bare Session in a test):
                    # resolve immediately as a denial, H2b's behaviour.
                    item["text"] = f"Permission requires interactive approval, unavailable in this session: {decision.reason}"
                    self._fire_permission_denied(name, tool_input, decision.reason)
                    return item
                # interactive: `_dispatch_tools` yields the `permission_request`
                # and then BLOCKS on this waiter until the UI answers (U2).
                item["pending_ask"] = True
                item["suggested_rule"] = decision.suggested_rule
                self._permission_waiters[tool_id] = {"event": threading.Event(), "decision": None}
                return item
            # decision.action == "allow" (the PermissionRequest hook just
            # answered it) -- fall through to item["ready"]=True below.
            # (decision.action == "deny" is unreachable here -- the plain
            # `if decision.action == "deny":` above already caught and
            # returned for every path that could produce it, including a
            # PreToolUse hook's own reassignment, which runs before it.)

        item["ready"] = True
        return item

    def _fire_permission_denied(self, name: str, tool_input: dict, reason: str) -> None:
        """H4 scope B: PermissionDenied -- observational only (nothing it
        returns can change an already-final denial)."""
        if self.hook_runner is None or not self.hook_runner.has_hooks("PermissionDenied"):
            return
        payload = self.hook_runner.payload("PermissionDenied", extra={"tool_name": name, "tool_input": tool_input,
                                                                        "reason": reason})
        try:
            self.hook_runner.run("PermissionDenied", payload, matched=name, tool_name=name, tool_input=tool_input)
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
        outcome = self.hook_runner.run(event, payload, matched=name, tool_name=name, tool_input=tool_input, tool=tool)
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
        by the time this is called)."""
        tool_id, name = item["tool_id"], item["name"]
        if not item["ready"]:
            text = item["text"]
            self.log.append_tool_result(tool_use_id=tool_id, content=text, is_error=True)
            yield events.Event("tool_result", {"id": tool_id, "ok": False, "summary": text}, turn=turn_no)
            if item.get("permission_denial") is not None:
                self.permission_denials.append(item["permission_denial"])
            return
        tr = item["result"]
        tr, hook_system_messages = self._apply_post_tool_use_hooks(name, tool_id, item["input"], tr)
        for msg in hook_system_messages:
            yield events.notification(msg)
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
        if count is not None and _LOOP_BREAKER_REMIND_AT <= count < _LOOP_BREAKER_DENY_AT:
            reminder = (f"\n\n[reminder: {name} has now been called with these same arguments "
                        f"{count} times this turn -- consider a different approach if unintentional]")
            if isinstance(content_for_log, list):
                content_for_log = content_for_log + [{"type": "text", "text": reminder.strip()}]
            else:
                content_for_log += reminder
            summary_text += reminder
        self.log.append_tool_result(tool_use_id=tool_id, content=content_for_log, is_error=tr.is_error)
        # review finding 15: `summary` alone (200 chars) is what Ctrl+O's
        # pager showed too, since `set_result` used to overwrite the
        # card's `body_text` with it -- `content` carries the FULLER
        # (still capped by spill_and_truncate/_mcp_blocks_for_log above,
        # e.g. tens of KB, never the raw uncapped tool output) text so
        # the pager has something real to show.
        yield events.Event("tool_result", {"id": tool_id, "ok": not tr.is_error, "summary": summary_text[:200],
                                            "content": summary_text}, turn=turn_no)

    # ---- interactive permission handshake (U2) ---------------------------

    def resolve_permission(self, request_id: str, decision) -> bool:
        """Called from the UI THREAD: answer the `permission_request` for
        `request_id`, unblocking the worker thread parked in
        `_await_permission_decision`. `decision` is a
        `rolo_claude.permissions.Decision` or a `{"action": ...}` dict
        (repaired by `set_permission_mode`/mode changes before this). Returns
        False when no such request is waiting (already answered/aborted)."""
        slot = self._permission_waiters.get(request_id)
        if slot is None:
            return False
        slot["decision"] = decision
        slot["event"].set()
        return True

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

    def _dispatch_tools(self, turn_no: int, tool_use_blocks: list, tool_call_flags: Optional[dict] = None) -> Iterator[events.Event]:
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
        ctx = ToolContext(cwd=self.cwd, read_cache=self._read_cache, abort=self.abort,
                           bash_state=self._bash_state, session_dir=self.log.dir / self.log.session_id,
                           registry=self.tool_registry, env=self.tool_env, catalog=self.session_catalog,
                           mcp_manager=self.mcp_manager,
                           session_allow_rule=lambda rule_text: self.permission_engine.add_session_allow_rule(
                               rule_text, temporary=True))
        repair_outcomes = repair_assistant_turn(tool_use_blocks, self.tool_registry, catalog=self.session_catalog)

        end_turn = False
        pending_batch: list = []  # `item` dicts: a run of consecutive READY read-only calls, dispatch deferred

        def _dispatch_pending_batch() -> None:
            """Actually RUN every item in `pending_batch` (fills in each
            `item["result"]`) -- does NOT log/yield anything; the caller
            still has to `_finalize_tool_result` each one itself, in order,
            same as a solo dispatch. finding 5 must-do: each call gets its
            OWN `tool_use_id` (the 3rd tuple element) so a pooled MCP
            result that needs to spill doesn't collide/silently skip."""
            calls = [(it["name"], it["input"], it["tool_id"]) for it in pending_batch]
            results = run_read_only_batch(self.tool_registry, calls, ctx)
            for it, tr in zip(pending_batch, results):
                it["result"] = tr

        for i, (tu, outcome) in enumerate(zip(tool_use_blocks, repair_outcomes)):
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
                for remaining_tu in tool_use_blocks[i:]:
                    r_id, r_name = remaining_tu.get("id"), remaining_tu.get("name")
                    interrupted_item = {"tool_id": r_id, "name": r_name, "input": remaining_tu.get("input") or {},
                                         "ready": False, "text": "Tool call interrupted by user", "repaired": False}
                    yield events.Event("tool_use_ready", {"id": r_id, "name": r_name,
                                                            "input": interrupted_item["input"], "repaired": False},
                                        turn=turn_no)
                    yield from self._finalize_tool_result(turn_no, interrupted_item, ctx.session_dir)
                return True

            item = self._resolve_tool_call(tu, outcome, tool_call_flags)
            tool_id, name = item["tool_id"], item["name"]

            yield events.Event("tool_use_ready", {"id": tool_id, "name": name, "input": item["input"],
                                                    "repaired": item["repaired"]}, turn=turn_no)
            for msg in item.pop("hook_system_messages", None) or []:
                yield events.notification(msg)
            if "ask_reason" in item:
                yield events.Event("permission_request", {"id": tool_id, "name": name, "input": item["input"],
                                                            "reason": item["ask_reason"],
                                                            "suggested_rule": item.get("suggested_rule")}, turn=turn_no)

            if item.get("pending_ask"):
                # U2: this really BLOCKS the worker thread until the UI's
                # PermissionCard answers (`answer_permission` ->
                # `resolve_permission`) or the abort Event fires.
                self._apply_permission_decision(item, self._await_permission_decision(tool_id))

            if item.get("pending_question"):
                # U2: the AskUserQuestion round trip -- never dispatched to
                # the tool itself; the UI's answer becomes its result.
                yield events.Event("question", {"id": tool_id, "name": name, "input": item["input"]}, turn=turn_no)
                answer = self._await_reply(self._question_waiters, tool_id)
                item.pop("pending_question", None)
                if answer is None:
                    item["text"] = "The user did not answer (the question was dismissed or the turn interrupted)."
                else:
                    item["result"] = ToolResult(answer if isinstance(answer, str)
                                                  else json.dumps(answer, ensure_ascii=False, default=str))
                    item["ready"] = True

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

            if item["ready"]:
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
            if item.get("end_turn"):
                end_turn = True
            if self._pending_steer():
                # review finding 6 / scope 0(c): checked per TOOL, not
                # just once per whole batch -- a steer that arrived while
                # this call was running stops any FURTHER call in this
                # same assistant turn from starting; the one that just
                # finished is kept (it already ran to completion).
                # `_turn_body`'s own post-dispatch check applies it.
                break

        if pending_batch:
            _dispatch_pending_batch()
            yield from self._fire_post_tool_batch(turn_no, pending_batch)
            for batched_item in pending_batch:
                yield from self._finalize_tool_result(turn_no, batched_item, ctx.session_dir)
                if batched_item.get("end_turn"):
                    end_turn = True
        return end_turn

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
        outcome = self.hook_runner.run("PostToolBatch", payload, matched="")
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
        same shape `_on_catalog_grow` already logs."""
        self.model_ref = model_ref
        self.model_profile = model_profile
        if creds is not None:
            self.creds = creds
        self.model_label = model_ref.raw
        self.route = Route(provider=model_ref.provider, upstream_model=model_ref.model, dialect=model_ref.dialect)
        self.provider_profile = resolve_profile(self.route)

        for name in self.tool_registry.names():
            tool = self.tool_registry.get(name)
            if hasattr(tool, "vision"):
                tool.vision = model_profile.vision
        if self.session_catalog is not None:
            from rolo_claude.agent.catalog import host_cap
            self.session_catalog.cap = host_cap(model_ref.provider)
            self.session_catalog.vision = model_profile.vision
            while (len(self.session_catalog.names) > self.session_catalog.cap
                   and self.session_catalog._evict_one()):
                pass
            self.log.append_meta(model=model_ref.raw,
                                  tools=self.tool_registry.definitions_for(self.session_catalog.names))
        else:
            self.log.append_meta(model=model_ref.raw, tools=self.tool_registry.definitions())

    def status_event(self, *, phase: str = "idle", context_tokens=None, turn: Optional[int] = None) -> events.Event:
        """The D-Contract `status` payload as THIS session knows it --
        permission_mode/session_id/context_limit filled from live state, so
        a UI never has to guess them. `mcp` comes from `self.mcp_status_fn`
        when the caller installed one (the Controller does; a bare Session
        reports 0/0)."""
        mcp = self.mcp_status_fn() if self.mcp_status_fn else {"connected": 0, "total": 0}
        return events.status(
            phase=phase, model=self.model_ref.raw, turn=self.turn_count if turn is None else turn,
            context_tokens=context_tokens, context_limit=self.model_profile.context_tokens,
            cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None,
            permission_mode=self.permission_engine.mode, session_id=self.log.session_id, mcp=mcp,
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
        queued) when no turn is running to steer."""
        if not text or not self.busy:
            return False
        with self._steer_lock:
            self._steer_queue.append(text)
        return True

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
        allowed to finish", scope 0c). Returns True iff at least one was
        applied, via the generator's return value (`yield from`) -- the
        caller must then `continue` its loop instead of ending the turn."""
        texts = self._pop_all_steers()
        for text in texts:
            yield events.steer_queued(text, turn=turn_no)
            self.log.append_user([{"type": "text", "text": text}])
            yield events.user_message(text, turn=turn_no)
            yield events.steer_applied(text, turn=turn_no)
        return bool(texts)

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
            out(events.turn_done(turn=self.turn_count, reason="error"))
            return

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
                out(self.status_event())
            elif kind == "permission_reply":
                # safety net only: the UI answers a pending request by
                # calling resolve_permission() directly (this branch is
                # unreachable while that wait is in flight).
                self.resolve_permission(data.get("id"), data.get("decision"))
            elif kind == "question_reply":
                self.resolve_question(data.get("id"), data.get("answer"))
            elif kind == "plan_reply":
                pass  # H6 wires plan mode; the card exists against this event

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
from rolo_claude.agent.derive import content_hash_from_oai_body, derive_request
from rolo_claude.agent.invariants import repair_truncated_text, synthesize_missing_results, validate_tool_use
from rolo_claude.agent.log import SessionLog
from rolo_claude.agent.repair import repair_assistant_turn
from rolo_claude.model import CostMeter, ModelProfile, ModelRef
from rolo_claude.permissions import Decision, PermissionEngine
from rolo_claude.providers.errors import CONTEXT_WINDOW_EXCEEDED, MAX_RETRIES, backoff_delay, is_reasoning_replay_bug
from rolo_claude.providers.hooks import (
    classify_length_tool_call, is_retryable_empty_completion, leak_parser,
    max_tokens_budget, overflow_classifier, record_databricks_output_tokens,
)
from rolo_claude.providers.config import tool_child_env
from rolo_claude.providers.profiles import ProviderProfile, resolve_profile
from rolo_claude.providers.request import ToolCatalogTooLarge, build_request_body
from rolo_claude.providers.routing import Route
from rolo_claude.providers.stream import (
    CompletionRequest, ContextOverflow, ProviderCreds, ProviderNotConfigured,
    UpstreamError, stream_completion,
)
from rolo_claude.tools.base import ToolContext, ToolResult
from rolo_claude.tools.registry import ToolRegistry, run_read_only_batch
from rolo_claude.tools.truncate import spill_and_truncate

log = logging.getLogger("bridge")

_MAX_RETRY_WAIT_S = 60.0  # per-wait cap, never the uncapped `time.sleep(Retry-After)` H0 had
_LOOP_BREAKER_REMIND_AT = 3
_LOOP_BREAKER_DENY_AT = 5
_LOOP_BREAKER_END_AT = 8
# H3 must-do: applied to a converted MCP tool result's TEXT, ahead of
# (and independent from) the tool's own `result_cap`/spill_and_truncate
# step -- an MCP server answering with a large resource/many blocks must
# not blow past this before the ordinary truncation path ever runs.
MAX_MCP_OUTPUT_TOKENS = 25_000

# A wire ("error" SSE event) carries an Anthropic-shaped `type` string, not
# an HTTP status -- reverses providers.errors.map_upstream_error's own
# status->type table so hooks.overflow_classifier (which wants a status)
# can still classify a wire error by the SAME taxonomy as a phase-1 one.
_WIRE_ERROR_TYPE_TO_STATUS = {
    "authentication_error": 401, "permission_error": 403, "not_found_error": 404,
    "invalid_request_error": 400, "rate_limit_error": 429, "api_error": 500,
    "overloaded_error": 503,
}


def _mcp_content_to_text(blocks: list) -> str:
    """Convert a list of MCP/Anthropic-shaped content blocks (a tool's
    `ToolResult.content` when it isn't a plain string -- see tools/base.py)
    into a readable TEXT representation for the transcript. H3 must-do:
    never a blind `json.dumps` of the raw block structure (which used to
    inline a whole base64 image payload as one giant JSON string, or
    render `default=str`-mangled Python reprs for anything non-JSON-safe)
    -- a text block's own text is used directly, an image/resource block
    becomes a short, honest placeholder (or a resource's own embedded
    text), and the whole thing is capped at MAX_MCP_OUTPUT_TOKENS."""
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif btype == "image":
            mime = block.get("mimeType") or ((block.get("source") or {}).get("media_type") if isinstance(block.get("source"), dict) else None) or "image"
            parts.append(f"[image: {mime}]")
        elif btype == "resource":
            res = block.get("resource") if isinstance(block.get("resource"), dict) else {}
            uri = res.get("uri") or block.get("uri") or "?"
            if isinstance(res.get("text"), str):
                parts.append(f"[resource {uri}]\n{res['text']}")
            else:
                parts.append(f"[resource: {uri}]")
        else:
            parts.append(f"[{btype or 'content'} block]")
    text = "\n".join(parts)
    limit = MAX_MCP_OUTPUT_TOKENS * 4  # this codebase's own len(text)/4 token estimate (providers/config.py)
    if len(text) > limit:
        text = text[:limit] + f"\n... [MCP result truncated at ~{MAX_MCP_OUTPUT_TOKENS} tokens]"
    return text


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
        self.cost_meter = CostMeter()
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

        self.log = session_log or SessionLog(cwd)
        existing_nodes = self.log.nodes()
        if not existing_nodes:
            self.log.append_meta(
                model=model_ref.raw, cwd=str(cwd),
                system_prompt_bytes=len(session_context.system_prompt.encode("utf-8")),
                tools=self.tool_registry.definitions(),
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

    # ---- request construction ------------------------------------------

    def _derive_and_build(self, tool_choice=None):
        # finding 4: tools=None makes derive_request fall back to the
        # logged meta node's FROZEN catalog, never the live registry --
        # what actually reached the model must match what a later replay
        # reconstructs, independent of whether the registry's tool
        # descriptions changed between this run and a resumed one.
        system_text, messages, tools = derive_request(self.log, tools=None)
        requested_max_tokens = max_tokens_budget(
            self.provider_profile, model_key=self.model_ref.raw, requested=None, abort=self.abort,
        )
        body = build_request_body(
            system_text=system_text, messages=messages, tools=tools, route=self.route,
            profile=self.provider_profile, effort=self.effort,
            context_tokens=self.model_profile.context_tokens,
            prompt_estimate=_rough_estimate(system_text, messages),
            requested_max_tokens=requested_max_tokens, tool_choice=tool_choice,
        )
        return system_text, messages, tools, body

    def _build_request(self, body: dict) -> CompletionRequest:
        return CompletionRequest(
            body={"messages": []}, route=self.route,
            profile={"context_tokens": self.model_profile.context_tokens,
                     "max_output_tokens": self.model_profile.max_output_tokens},
            creds=self.creds, state_dir=self.state_dir, extra_headers=self.extra_headers,
            model_label=self.model_ref.raw, openrouter_base_url=self.openrouter_base_url,
            harness_mode=True, prebuilt_oai_body=body,
            ping_interval=float(os.environ.get("BRIDGE_PING_INTERVAL", "15")),
            tool_id_format=self.provider_profile.tool_id_format,
        )

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

    def _step(self, turn_no: int, tool_choice=None):
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
            _, _, _, body = self._derive_and_build(tool_choice=tool_choice)
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
            gen = stream_completion(req, abort=self.abort)
            assistant_blocks: list = []
            partial_json: dict = {}
            stop_reason = None
            usage: dict = {}
            harness_meta: dict = {}
            wire_error: Optional[dict] = None
            phase1_failure: Optional[Exception] = None
            try:
                for ev in gen:
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
                # U2: an interrupt (Esc/Ctrl+C in the TUI, or a UI `quit`)
                # cut this call short -- never retry and never dress it up
                # as a provider failure; `_turn_body` reports it as
                # `turn_done(reason="interrupted")`.
                return None

            if phase1_failure is not None:
                if isinstance(phase1_failure, ContextOverflow):
                    e = phase1_failure
                    yield events.error(
                        f"context window overflow (limit={e.limit} tokens, prompt~={e.prompt_tokens}) -- "
                        f"compaction isn't implemented yet in this build",
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
                if e.retryable and attempts <= MAX_RETRIES:
                    delay = min(backoff_delay(attempts - 1, e.retry_after), _MAX_RETRY_WAIT_S)
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
                if retryable and attempts <= MAX_RETRIES:
                    delay = min(backoff_delay(attempts - 1, wire_error.get("retry_after")), _MAX_RETRY_WAIT_S)
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

    def _turn_body(self, turn_no: int) -> Iterator[events.Event]:
        # finding 9: `--max-turns` counts MODEL CALLS made WITHIN this one
        # turn (Claude Code semantics) -- `-p` calls `turn()` exactly once,
        # so counting turn() calls against it (the pre-H2 behavior) never
        # bounded anything inside a tool loop; only the identical-call
        # breaker (a SEPARATE guard, kept as-is) did.
        model_calls = 0
        while True:
            if model_calls >= self.max_turns:
                yield events.status(phase="idle", model=self.model_ref.raw, turn=turn_no,
                                     cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None)
                yield events.turn_done(turn=turn_no, reason="max_turns")
                return

            result = yield from self._step(turn_no)
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
                yield events.status(phase="idle", model=self.model_ref.raw, turn=turn_no, context_tokens=input_tokens,
                                     context_limit=self.model_profile.context_tokens,
                                     cost_usd=self.cost_meter.total_usd if self.cost_meter.has_cost_data else None)
                # finding 3: a message's OWN max_tokens cutoff is not the
                # SESSION hitting --max-turns -- report it as what it is.
                reason = "max_tokens" if result.stop_reason == "max_tokens" else "end_turn"
                yield events.turn_done(turn=turn_no, reason=reason)
                return

            ended = yield from self._dispatch_tools(turn_no, tool_use_blocks, result.tool_call_flags)
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
        if decision.action == "deny":
            text = f"Permission denied: {decision.reason}"
            if decision.suggested_rule:
                text += f" (suggested rule: {decision.suggested_rule})"
            item["text"] = text
            item["permission_denial"] = decision.permission_denial
            return item
        if decision.action == "ask":
            item["ask_reason"] = decision.reason
            if not self.interactive:
                # no UI is attached (print mode / a bare Session in a test):
                # resolve immediately as a denial, H2b's behaviour.
                item["text"] = f"Permission requires interactive approval, unavailable in this session: {decision.reason}"
                return item
            # interactive: `_dispatch_tools` yields the `permission_request`
            # and then BLOCKS on this waiter until the UI answers (U2).
            item["pending_ask"] = True
            item["suggested_rule"] = decision.suggested_rule
            self._permission_waiters[tool_id] = {"event": threading.Event(), "decision": None}
            return item

        item["ready"] = True
        return item

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
        if isinstance(tr.content, str):
            content_text = tr.content
        elif isinstance(tr.content, list):
            # H3 must-do: MCP image/resource blocks converted properly,
            # never json.dumps'd as a raw blob (see _mcp_content_to_text).
            content_text = _mcp_content_to_text(tr.content)
        else:
            content_text = str(tr.content)
        content_text = spill_and_truncate(content_text, cap=self.tool_registry.result_cap(name),
                                           session_dir=session_dir, tool_use_id=tool_id)
        count = item.get("count")
        if count is not None and _LOOP_BREAKER_REMIND_AT <= count < _LOOP_BREAKER_DENY_AT:
            content_text += (f"\n\n[reminder: {name} has now been called with these same arguments "
                              f"{count} times this turn -- consider a different approach if unintentional]")
        self.log.append_tool_result(tool_use_id=tool_id, content=content_text, is_error=tr.is_error)
        yield events.Event("tool_result", {"id": tool_id, "ok": not tr.is_error, "summary": content_text[:200]}, turn=turn_no)

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
        None on abort/timeout/never-registered."""
        slot = waiters.pop(request_id, None)
        if slot is None:
            return None
        while not slot["event"].wait(0.1):
            if self.abort.is_set():
                return None
            if timeout is not None:
                timeout -= 0.1
                if timeout <= 0:
                    return None
        return slot["answer"] if "answer" in slot else slot["decision"]

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
                           registry=self.tool_registry, env=self.tool_env)
        repair_outcomes = repair_assistant_turn(tool_use_blocks, self.tool_registry)

        end_turn = False
        pending_batch: list = []  # `item` dicts: a run of consecutive READY read-only calls, dispatch deferred

        def _dispatch_pending_batch() -> None:
            """Actually RUN every item in `pending_batch` (fills in each
            `item["result"]`) -- does NOT log/yield anything; the caller
            still has to `_finalize_tool_result` each one itself, in order,
            same as a solo dispatch."""
            calls = [(it["name"], it["input"]) for it in pending_batch]
            results = run_read_only_batch(self.tool_registry, calls, ctx)
            for it, tr in zip(pending_batch, results):
                it["result"] = tr

        for tu, outcome in zip(tool_use_blocks, repair_outcomes):
            item = self._resolve_tool_call(tu, outcome, tool_call_flags)
            tool_id, name = item["tool_id"], item["name"]

            yield events.Event("tool_use_ready", {"id": tool_id, "name": name, "input": item["input"],
                                                    "repaired": item["repaired"]}, turn=turn_no)
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
                progress_chunks: list = []
                item_ctx = dataclasses.replace(ctx, tool_use_id=tool_id, progress_cb=progress_chunks.append)
                item["result"] = self.tool_registry.dispatch(name, item["input"], item_ctx)
                for chunk in progress_chunks:
                    yield events.Event("tool_progress", {"id": tool_id, "name": name, "text": chunk}, turn=turn_no)
            yield from self._finalize_tool_result(turn_no, item, ctx.session_dir)
            if item.get("end_turn"):
                end_turn = True

        if pending_batch:
            _dispatch_pending_batch()
            for batched_item in pending_batch:
                yield from self._finalize_tool_result(turn_no, batched_item, ctx.session_dir)
                if batched_item.get("end_turn"):
                    end_turn = True
        return end_turn

    # ---- the interactive command pump (U2, D-Contract) -------------------

    def set_model(self, model_ref: ModelRef, model_profile: ModelProfile, creds=None) -> None:
        """Swap the active model mid-session (`/model`): the ref, its
        profile and (when given) its credentials, plus the derived Route/
        provider profile every request builder reads. The LOGGED frozen
        catalog is deliberately untouched -- `derive_request` keeps
        reconstructing the tools the session actually started with."""
        self.model_ref = model_ref
        self.model_profile = model_profile
        if creds is not None:
            self.creds = creds
        self.model_label = model_ref.raw
        self.route = Route(provider=model_ref.provider, upstream_model=model_ref.model, dialect=model_ref.dialect)
        self.provider_profile = resolve_profile(self.route)

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
            kind = getattr(cmd, "kind", None)
            data = getattr(cmd, "data", None) or {}
            if kind == "user_input":
                self._pump_turn(data.get("text", ""), data.get("images"), out)
            elif kind == "interrupt":
                self.abort.set()
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

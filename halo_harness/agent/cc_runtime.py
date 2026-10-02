"""halo_harness.agent.cc_runtime -- H11 Part B: turn execution for a `cc:`
route Session -- lazily starts one `ClaudeCodeProcess` + `ToolBridgeServer`
pair per session, drives one turn (stream-json user line in, events out),
and runs the bridge's own "normal dispatch path" for every
`mcp__rolo__<Name>` call Claude Code makes.

H11b rewrite: the bridge's dispatch is now LITERALLY the loop's own
dispatch -- `bridge_call_tool` builds a trivial (already-valid, Claude
Code's own MCP client validated it) `RepairOutcome` and calls
`Session._resolve_tool_call` + `Session._finalize_tool_result` directly,
so permission decide, PreToolUse/PermissionRequest/PermissionDenied
hooks, the loop breaker, EnterPlanMode/ExitPlanMode, AskUserQuestion and
Agent/Task all behave EXACTLY as they do for every other route (findings
3, 13, 17) -- see `_resolve_and_dispatch_bridged_call`'s own docstring.

Steering (critical finding 1): claude runs with `--replay-user-messages`,
so every stdin line we send -- the turn's own line and any steer -- is
echoed back verbatim as `{"type":"user","isReplay":true,...}` once claude
actually starts consuming it (live-verified against 2.1.284: a line sent
while claude is still busy is NOT echoed until claude is ready for it,
which may be well after an EARLIER line's own `result`). `CcState.
unconfirmed` is a FIFO of lines sent but not yet echoed; a rolo turn ends
at a `result` only once that FIFO is empty -- correct whether claude
absorbs a steer into the turn already running (one result covers both,
FIFO empties before that one result) or answers it as a genuine follow-up
turn (a `result` that arrives while the FIFO still isn't empty is NOT the
end; more is coming).

Threading model, per turn: the CALLING thread (the Session's own worker
thread) runs `turn_body_cc`; a background READER thread parses stdout
into events; a background WATCHER thread turns `session.abort` into a
subprocess interrupt; the bridge's own connection thread(s) run
`bridge_call_tool` synchronously whenever Claude Code calls a tool. All
three feed ONE per-turn queue so `yield` order matches real-time arrival.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid as _uuid_mod
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from halo_harness import events
from halo_harness.agent.cc_process import ClaudeCodeProcess, build_cc_argv, build_mcp_config
from halo_harness.ccbridge.server import ToolBridgeServer
from halo_harness.providers.cc_models import ClaudeCodeNotFoundError, SUBSCRIPTION_AUTH_METHODS, claude_auth_status, \
    resolve_claude_launch_argv
from halo_harness.tools.base import ToolContext

log = logging.getLogger("bridge")

_ABORT_POLL_S = 0.05
_KILL_GRACE_S = 3.0
_TOOL_USE_ID_WAIT_S = 5.0
_RESUME_PROBE_S = 0.25          # finding 9: bounded wait to catch an immediate --resume/--session-id failure
_MAX_PRIME_CHARS = 60_000       # finding 10: "cap the primer" -- ~15k tokens of tail, never the whole log unbounded
_RESUME_FAILURE_MARKERS = ("no conversation found", "already in use")


class ClaudeCodeUnavailable(Exception):
    """`cc:` was requested but `claude` isn't installed, isn't logged in,
    or is logged in with something other than a claude.ai subscription
    (critical finding 2) -- the brief's "precise one-line errors" case.
    Never raised for a transient failure (a dead subprocess mid-session is
    handled by restarting with --resume, not this)."""


@dataclass
class CcState:
    process: ClaudeCodeProcess
    bridge: ToolBridgeServer
    cc_session_id: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    active_queue: "Optional[queue.Queue]" = None
    pending_tool_uses: list = field(default_factory=list)
    tool_use_cond: "Optional[threading.Condition]" = None
    # tool_use ids `bridge_call_tool` is CURRENTLY dispatching -- see
    # module docstring / the original H11 design note this preserves.
    in_flight: set = field(default_factory=set)
    cleanup_thread: "Optional[threading.Thread]" = None
    # critical finding 1: FIFO of lines sent but not yet echoed back by
    # claude (`--replay-user-messages`) -- see module docstring. Each
    # entry is {"kind": "turn"|"context"|"steer", "text": str}; only a
    # "steer" entry gets logged (as a session-log "steer" node) at the
    # moment it's actually confirmed consumed, never at send time.
    unconfirmed: "deque" = field(default_factory=deque)
    session_id_confirmed: bool = False
    turn_is_error: bool = False     # finding 12: set when a `result` carries is_error -- read at DONE
    turn_had_deltas: bool = False   # finding 12: reset per claude-native message_start
    last_total_cost: "Optional[float]" = None  # finding 11: result.total_cost_usd is CUMULATIVE for the process

    def __post_init__(self) -> None:
        self.tool_use_cond = threading.Condition(self.lock)


# 2.0.1 W3a: "the cc: route prints one line on first use outside the
# [tested] range" -- doctor's own check (halo_harness/doctor.py) is the
# rich, always-on version of this; this is the SAME data (providers/
# cc_tested.json) consulted once per PROCESS, the first time a cc: session
# actually starts, for whoever never runs `halo doctor` at all. A plain
# module-level flag (never reset in production -- a test resets it
# directly, same convention as cc_models.reset_cached_claude_auth_status).
_version_range_notice_emitted = False


def _maybe_warn_cc_version_outside_tested_range() -> None:
    """Informational only -- never raises, never blocks/gates `cc:` itself."""
    global _version_range_notice_emitted
    if _version_range_notice_emitted:
        return
    _version_range_notice_emitted = True
    try:
        from halo_harness.providers.cc_models import (
            installed_claude_version, load_cc_tested_range, version_outside_tested_range,
        )
        version = installed_claude_version()
        tested = load_cc_tested_range()
        if version_outside_tested_range(version, tested=tested):
            log.warning("cc: installed claude %s is newer than the tested range (%s-%s, verified %s) -- "
                        "watch for behavior changes", version, tested.get("min"), tested.get("max"),
                        tested.get("date"))
    except Exception:
        pass


def _preflight_cc() -> Optional[str]:
    """None when `cc:` is usable right now; else a precise one-line
    reason. Critical finding 2: `claude auth status` is checked in the
    SAME stripped environment (`cc_child_env`) the real subprocess gets,
    and `authMethod` must be `claude.ai` -- an ambient ANTHROPIC_API_KEY
    (or a Claude Code login that is itself only an api_key, never a real
    subscription) is refused with a message naming `ant:` explicitly,
    never silently treated as "available"."""
    try:
        resolve_claude_launch_argv()
    except ClaudeCodeNotFoundError:
        return ("cc: models need Claude Code installed (https://claude.com/claude-code) -- "
                 "install it, run `claude` once to log in, then try again.")
    status = claude_auth_status()
    if status is None:
        return ("cc: models need Claude Code installed (https://claude.com/claude-code) -- "
                 "install it, run `claude` once to log in, then try again.")
    if getattr(status, "timed_out", False):
        return "cc: could not check `claude auth status` (it timed out) -- check the claude binary and try again."
    if not status.logged_in:
        key_hint = " (an ANTHROPIC_API_KEY is set -- that's the ant: route; use ant:<model> now)" \
            if os.environ.get("ANTHROPIC_API_KEY") else ""
        return f"cc: models need a Claude subscription login -- run `claude` once and log in, then try again.{key_hint}"
    if status.auth_method not in SUBSCRIPTION_AUTH_METHODS:
        return (f"cc: would not use the subscription -- `claude auth status` reports {status.auth_method!r}, "
                 f"not a claude.ai login (that's the ant: route: use ant:<model> to spend an API key instead). "
                 f"Run `claude` once, logged into claude.ai with no ANTHROPIC_API_KEY set, for cc:.")
    return None


def _cc_child_env(session) -> dict:
    from halo_harness.providers.config import cc_child_env
    base = getattr(session, "tool_env", None) or dict(os.environ)
    return cc_child_env(base)


def _last_cc_session_id(log) -> Optional[str]:
    """H11b finding 9: the cc conversation id to `--resume` -- the LAST
    `meta` node carrying one, logged by `ensure_cc_state`/the reader's
    own session_id-confirmation step every time a claude process starts
    or its real (possibly claude-assigned, see `--fork-session`) id is
    learned. None for a rolo session that never ran a `cc:` turn."""
    for node in reversed(log.nodes()):
        if node.get("type") == "meta" and node.get("cc_session_id"):
            return node["cc_session_id"]
    return None


def ensure_cc_state(session) -> CcState:
    """Lazily start (or, after a dead epoch -- Esc/crash -- restart with
    `--resume`) this session's ONE claude subprocess + bridge server
    pair. Findings 8/9/10: the id to `--resume` comes from the LOG (a
    `cc_session_id` meta node), never a value re-derived from the rolo
    session id -- `/clear` drops it (a fresh log has none), `/fork`
    marks `_cc_fork_session` so this restarts with `--fork-session`, and
    a resume that fails ("No conversation found"/"already in use") falls
    back to a genuinely fresh `--session-id` primed with the prior log."""
    state: Optional[CcState] = getattr(session, "_cc_state", None)
    if state is not None:
        cleanup = state.cleanup_thread
        if cleanup is not None and cleanup.is_alive():
            cleanup.join(timeout=_KILL_GRACE_S + 3.0)
        if state.process.alive:
            # finding 10: a cc:->or:->cc: round trip reuses this SAME
            # still-alive process -- drain whatever `set_model`/
            # `prepare_conversation_so_far` stashed while we were away
            # BEFORE handing the (possibly tool-calling) process back to
            # a real turn, so it is never silently dropped.
            _drain_pending_context(session, state)
            return state

    err = _preflight_cc()
    if err:
        raise ClaudeCodeUnavailable(err)
    # 2.0.1 W3a: same thread _preflight_cc() just spawned `claude auth
    # status` on (the session's own worker thread, never the UI thread) --
    # one more one-shot subprocess here is no new UI-thread risk.
    _maybe_warn_cc_version_outside_tested_range()

    old_bridge = state.bridge if state is not None else None
    fork_requested = bool(getattr(session, "_cc_fork_session", False))
    session._cc_fork_session = False
    prior_id = _last_cc_session_id(session.log)
    if prior_id:
        conversation_id, resume, fork_session = prior_id, True, fork_requested
    else:
        conversation_id, resume, fork_session = str(_uuid_mod.uuid4()), False, False

    state, failed = _start_cc_process(session, conversation_id=conversation_id, resume=resume,
                                       fork_session=fork_session)
    if failed and resume:
        # finding 9: an immediate resume failure (claude never got past
        # argument validation) -- fall back to a genuinely fresh
        # conversation, primed with the prior log, so THIS turn still
        # succeeds instead of surfacing "No conversation found"/"already
        # in use" to the user. Any OTHER immediate death (not one of
        # those two shapes, and not a fork) is retried identically once;
        # `turn_body_cc`'s own EOF handling (with stderr_tail) reports it
        # if that also fails.
        stderr = state.process.stderr_tail().lower()
        _kill_and_close(state)
        if fork_session or any(marker in stderr for marker in _RESUME_FAILURE_MARKERS):
            session._cc_pending_context = _render_conversation_so_far(session)
            state, failed = _start_cc_process(session, conversation_id=str(_uuid_mod.uuid4()),
                                               resume=False, fork_session=False)
        else:
            state, failed = _start_cc_process(session, conversation_id=conversation_id, resume=resume,
                                               fork_session=fork_session)

    if old_bridge is not None:
        try:
            old_bridge.close()
        except Exception:
            pass
    session._cc_state = state
    if not fork_session:
        # a fork's real (claude-assigned) id is unknown until the first
        # stream-json line reports its own `session_id` -- see
        # `_confirm_session_id`, called from the reader/priming drain.
        session.log.append_meta(cc_session_id=state.cc_session_id, cc_resumed=resume)
    _drain_pending_context(session, state)
    return state


def _kill_and_close(state: CcState) -> None:
    try:
        state.process.kill()
        state.process.wait(timeout=2.0)
    except Exception:
        pass
    try:
        state.bridge.close()
    except Exception:
        pass


def _drain_pending_context(session, state: CcState) -> None:
    pending_ctx = getattr(session, "_cc_pending_context", None)
    if pending_ctx:
        session._cc_pending_context = None
        _prime_with_context(state, pending_ctx, session)


def _cc_system_addendum(session) -> str:
    """H11b finding 5: rolo's own prompt pieces, ADDED to claude's default
    system prompt via `--append-system-prompt` -- never replacing it.
    Claude Code still loads CLAUDE.md/memory/its own skill descriptions
    itself (brief's binding v1 behaviour: rolo does NOT inject instruction
    /memory snapshots for cc: turns), so none of those are repeated here
    -- only things Claude Code has no other way to learn: which bare
    skill/subagent_type names resolve to something, MCP server-level
    instructions, deferred-tool names, the plan-mode note, and (for a
    sub-agent CHILD session) its own specific body/description, which
    REPLACES the generic bridged-tools blurb entirely (a child's whole
    `session_context.system_prompt` already IS that body -- agent/
    subagent.py's `_build_child_session`)."""
    from halo_harness.agent.cc_process import CC_APPEND_SYSTEM_PROMPT

    # critical live finding (not in the original 28, caught by this pass's
    # own Part D acceptance run): `--append-system-prompt <value>` rides
    # on claude's OWN command line -- on Windows that's `claude.CMD` ->
    # cmd.exe -> claude.exe, and cmd.exe's practical argv budget is far
    # below the ~32K CreateProcess allows (verified live: a real ~12-skill,
    # verbosely-described dev box hit "The command line is too long" and
    # the claude subprocess never started at all). Every discovered name's
    # own description is clipped hard, and the whole addendum is capped
    # again after assembly -- a short, useful hint beats a crashed session.
    _NAME_DESC_CHARS = 100
    _ADDENDUM_CHARS = 3500

    parts = []
    if getattr(session, "agent_id", None):
        body = (getattr(session.session_context, "system_prompt", "") or "").strip()
        if body:
            parts.append(body[:_ADDENDUM_CHARS])
    parts.append(CC_APPEND_SYSTEM_PROMPT)

    try:
        from halo_harness.commands.skills import discover_all_skills
        skills = discover_all_skills(session.cwd)
        if skills:
            lines = [f"- {name}: {(getattr(cmd, 'description', '') or '')[:_NAME_DESC_CHARS]}"
                      for name, cmd in sorted(skills.items())]
            parts.append("Skills available via the Skill tool:\n" + "\n".join(lines))
    except Exception:
        pass

    agents = getattr(getattr(session, "agent_runtime", None), "agents", None) or {}
    if agents:
        lines = [f"- {name}: {(spec.description or '')[:_NAME_DESC_CHARS]}" for name, spec in sorted(agents.items())]
        parts.append("subagent_type values the Agent tool accepts:\n" + "\n".join(lines))

    if session.mcp_manager is not None:
        try:
            for entry in session.mcp_manager.status():
                instr = (entry.get("instructions") or "").strip()
                if instr:
                    parts.append(f"MCP server {entry.get('name')!r} instructions:\n{instr[:_NAME_DESC_CHARS * 4]}")
        except Exception:
            pass

    if session.session_catalog is not None and session.session_catalog.deferred:
        deferred_names = sorted(session.session_catalog.deferred)
        parts.append(
            "The following deferred tools are now available via ToolSearch. Their schemas "
            "are NOT loaded -- calling them directly will fail with InputValidationError. Use "
            "ToolSearch with query \"select:<name>[,<name>...]\" to load tool schemas before "
            "calling them:\n" + "\n".join(deferred_names)
        )

    if session.permission_engine.mode == "plan":
        from halo_harness.agent.planmode import PLAN_MODE_NOTE
        parts.append(PLAN_MODE_NOTE)

    text = "\n\n".join(p for p in parts if p)
    if len(text) > _ADDENDUM_CHARS:
        text = text[:_ADDENDUM_CHARS] + "\n...(addendum truncated to stay well under the OS command-line limit)"
    return text


def _start_cc_process(session, *, conversation_id: str, resume: bool, fork_session: bool) -> "tuple[CcState, bool]":
    """Builds+starts a fresh bridge+process pair and returns `(state,
    failed)` -- `failed` is True only when `resume` was requested and the
    process already exited within a short probe window (claude rejects a
    bad `--resume`/`--session-id` near-instantly, well before it would
    ever print a single stream-json line for a genuinely slow-but-normal
    startup)."""
    bridge = ToolBridgeServer(session_id=str(_uuid_mod.uuid4()), list_tools_fn=lambda: bridge_list_tools(session),
                               call_tool_fn=lambda name, arguments: bridge_call_tool(session, name, arguments))
    bridge.start()
    mcp_config = build_mcp_config({})
    argv = build_cc_argv(model=session.model_ref.model, session_id=conversation_id, resume=resume,
                          mcp_config=mcp_config, fork_session=fork_session,
                          append_system_prompt=_cc_system_addendum(session),
                          max_turns=getattr(session, "max_turns", None), effort=getattr(session, "effort", None))
    env = _cc_child_env(session)
    env.update(bridge.child_env())
    process = ClaudeCodeProcess(argv, cwd=session.cwd, env=env)
    state = CcState(process=process, bridge=bridge, cc_session_id=conversation_id)
    if resume:
        time.sleep(_RESUME_PROBE_S)
        if not process.alive:
            return state, True
    return state, False


def _record_tool_use_announcements(state: CcState, obj: dict) -> None:
    """Shared by the reader (`_events_for_stdout_obj`) and the priming
    drain (`_prime_with_context`, finding 10: a tool call triggered by a
    huge conversation-so-far dump must still be correlatable) -- appends
    every `tool_use` block in an `assistant` message to the FIFO
    `_take_tool_use_id` matches against."""
    from halo_harness.mcp.manager import split_mcp_tool_name
    for b in (obj.get("message") or {}).get("content") or []:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            raw_name = b.get("name") or ""
            split = split_mcp_tool_name(raw_name)
            bare_name = split[1] if split else raw_name
            with state.lock:
                state.pending_tool_uses.append({"id": b.get("id"), "name": bare_name, "input": b.get("input") or {}})
                state.tool_use_cond.notify_all()


def _confirm_session_id(session, state: CcState, obj: dict) -> None:
    """H11b finding 8: a `--fork-session` restart doesn't get to CHOOSE
    the new conversation id (claude assigns one itself) -- every
    stream-json line carries `session_id`, so the first one we ever see
    from a freshly (re)started process is read back here and logged as
    the authoritative `cc_session_id`, whether or not it matches what we
    originally asked for."""
    if state.session_id_confirmed:
        return
    real_id = obj.get("session_id")
    if not isinstance(real_id, str) or not real_id:
        return
    state.session_id_confirmed = True
    if real_id == state.cc_session_id:
        # the ordinary (non-fork) case: `ensure_cc_state` already logged
        # this exact id before the process ever started -- nothing NEW to
        # record, just confirmed. Only a `--fork-session` restart (which
        # deliberately skips that up-front log, since the real id isn't
        # known until now) reaches the write below.
        return
    state.cc_session_id = real_id
    session.log.append_meta(cc_session_id=real_id, cc_resumed=True)


def _render_conversation_so_far(session) -> str:
    """H11b finding 10: a CAPPED tail (never the whole log unbounded) --
    Claude Code compacts its own context once it has enough of one, but a
    huge one-shot prime is still needless latency/spend for a message
    meant only to "catch up" a fresh/reused process."""
    try:
        from halo_harness.controller import render_transcript_markdown
        text = render_transcript_markdown(session.log.nodes(), session_id=session.log.session_id)
    except Exception:
        text = ""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > _MAX_PRIME_CHARS:
        text = "...(earlier conversation omitted)...\n\n" + text[-_MAX_PRIME_CHARS:]
    return (
        "<conversation-so-far>\n" + text + "\n</conversation-so-far>\n\n"
        "(This is context carried over from before this session switched models -- "
        "no reply needed here; just wait for the next actual message.)"
    )


def prepare_conversation_so_far(session) -> None:
    """Called from `Session.set_model` when switching INTO cc: mid-session
    (provider was something else, now "cc"). Stashes the rendered prior
    transcript for `ensure_cc_state`'s NEXT prime (a fresh start, or a
    still-alive reused process it drains into right away)."""
    text = _render_conversation_so_far(session)
    if text:
        session._cc_pending_context = text


def _prime_with_context(state: CcState, text: str, session) -> None:
    """H11 Part B: sent and drained silently (never queued/logged as a
    normal turn) before the session's next REAL cc: turn ever runs.
    Finding 10: any ask this triggers is auto-denied -- `session.
    interactive` is flipped False for the duration, reusing `_resolve_
    tool_call`'s own existing "no UI attached" immediate-denial path
    (agent/loop.py) instead of a bespoke one, so nothing ever blocks here
    waiting for a card that has nowhere to be shown."""
    was_interactive = session.interactive
    session.interactive = False
    try:
        state.process.send_user_line(text)
        while True:
            ev = state.process.read_event()
            if ev is None:
                return
            _confirm_session_id(session, state, ev)
            typ = ev.get("type")
            if typ == "assistant":
                _record_tool_use_announcements(state, ev)
                continue
            if typ == "result":
                return
    except (BrokenPipeError, OSError):
        return
    finally:
        session.interactive = was_interactive


def close_cc(session) -> None:
    """Kills the subprocess (process group) and closes the bridge server
    -- safe to call any number of times, including when `cc:` was never
    used this session at all. Finding 14: also walks any live cc: sub-
    agent children this session's own AgentRuntime still knows about
    (belt-and-suspenders past `run_agent_call`/`_bg_run`'s own `finally`,
    agent/subagent.py) so quitting the whole process never leaves an
    orphaned child claude/bridge/socket behind."""
    runtime = getattr(session, "agent_runtime", None)
    if runtime is not None:
        for child in list(getattr(runtime, "live_children", {}).values()):
            close_cc(child)
    state: Optional[CcState] = getattr(session, "_cc_state", None)
    if state is None:
        return
    try:
        state.process.interrupt()
        if state.process.wait(timeout=_KILL_GRACE_S) is None:
            state.process.kill()
            state.process.wait(timeout=2.0)
    except Exception:
        pass
    try:
        state.bridge.close()
    except Exception:
        pass


def notify_catalog_changed(session) -> None:
    """H11b finding 6: bumps the bridge's own generation counter so its
    child's long poll (`tools/await_change`) wakes and sends Claude Code
    a real `notifications/tools/list_changed` -- a no-op when `cc:` was
    never used this session (no bridge to notify)."""
    state: Optional[CcState] = getattr(session, "_cc_state", None)
    if state is not None:
        state.bridge.notify_tools_changed()


def one_shot_cc_call(model: str, system_text: str, user_text: str, *, timeout_s: float = 60.0) -> str:
    """H11b finding 22: a quick, STATELESS `claude -p` call for
    `Session.call_small_model`'s cc: branch (title generation, prompt/
    agent hooks, `/improve`'s drafting call) -- never touches any
    session's own live `_cc_state`/conversation. Raises RuntimeError on
    failure, the same contract `call_small_model`'s HTTP branch already
    has (its own callers already handle it -- e.g. "falling back to a
    heuristic on any failure")."""
    import subprocess
    try:
        argv = resolve_claude_launch_argv()
    except ClaudeCodeNotFoundError as e:
        raise RuntimeError(f"cc: small-model call unavailable: {e}") from e
    from halo_harness.providers.config import cc_child_env
    env = cc_child_env(dict(os.environ))
    argv = argv + [
        "-p", "--model", model, "--output-format", "json", "--max-turns", "1",
        "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--no-session-persistence", "--permission-mode", "bypassPermissions",
        "--append-system-prompt", system_text, user_text,
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, env=env)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"cc: small-model call timed out after {timeout_s}s") from e
    except OSError as e:
        raise RuntimeError(f"cc: small-model call failed to start: {e}") from e
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"cc: small-model call produced no parseable output (exit {proc.returncode})") from e
    if data.get("is_error"):
        raise RuntimeError(f"cc: small-model call failed: {data.get('result') or 'unknown error'}")
    return data.get("result") or ""


# ---- bridge tools/list + tools/call (runs on the bridge server's own
# connection thread, concurrently with turn_body_cc's reader below) -------

def bridge_list_tools(session) -> list:
    """The session's CURRENT catalog (built-ins, MCP tools, ToolSearch,
    Agent, plan-mode tools, AskUserQuestion, TodoWrite, WebFetch/
    BashOutput/TaskStop) -- findings 6/7: also carries `annotations.
    readOnlyHint`/`destructiveHint` and an MCP tool's own `_meta` dict
    (both dropped by the plain `Tool.definition()` every OTHER route's
    wire request also uses -- adding them there would touch every
    provider's own prompt-cache-stable tool list; the bridge's own
    `tools/list` answer is the only place they need to ride along)."""
    if session.session_catalog is not None:
        defs = session.tool_registry.definitions_for(session.session_catalog.names)
    else:
        defs = session.tool_registry.definitions()
    out = []
    for d in defs:
        tool = session.tool_registry.get(d.get("name", ""))
        entry = dict(d)
        if tool is not None:
            if getattr(tool, "is_read_only", False) or getattr(tool, "is_destructive", False):
                entry["annotations"] = {"readOnlyHint": bool(getattr(tool, "is_read_only", False)),
                                          "destructiveHint": bool(getattr(tool, "is_destructive", False))}
            meta = getattr(tool, "meta", None)
            if meta:
                entry["_meta"] = meta
        out.append(entry)
    return out


def _emit(session, ev: "events.Event") -> None:
    state: Optional[CcState] = getattr(session, "_cc_state", None)
    q = state.active_queue if state is not None else None
    if q is not None:
        q.put(ev)


def _take_tool_use_id(session, name: str, tool_input: dict) -> str:
    """Correlates a `tools/call` (name+arguments) back to Claude Code's
    OWN `toolu_...` id, announced earlier on the stream-json channel --
    matched on (name, input) (finding 20: name-only matching let an
    announced-but-never-called bare tool -- e.g. a rejected bare `Read`,
    which claude's own validation refuses -- steal the FIFO slot of the
    next REAL same-name call), woken by the reader's own condition
    variable instead of a fixed poll (finding 20), with a bounded wait so
    a call is never dropped -- a synthetic id is the last resort."""
    state: CcState = session._cc_state
    deadline = time.monotonic() + _TOOL_USE_ID_WAIT_S
    with state.tool_use_cond:
        while True:
            for i, pending in enumerate(state.pending_tool_uses):
                if pending.get("name") == name and pending.get("input") == (tool_input or {}):
                    del state.pending_tool_uses[i]
                    return pending.get("id") or f"ccbridge_{_uuid_mod.uuid4().hex[:12]}"
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not state.tool_use_cond.wait(timeout=min(remaining, 0.25)):
                if time.monotonic() >= deadline:
                    # last resort: a same-name entry with different input
                    # (e.g. claude re-announced with repaired args) beats
                    # a synthetic id with no relation to the real call.
                    for i, pending in enumerate(state.pending_tool_uses):
                        if pending.get("name") == name:
                            del state.pending_tool_uses[i]
                            return pending.get("id") or f"ccbridge_{_uuid_mod.uuid4().hex[:12]}"
                    return f"ccbridge_{_uuid_mod.uuid4().hex[:12]}"


def _wire_result(text: str, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": bool(is_error)}


def _wire_result_from_content(content, is_error: bool) -> dict:
    """finding 7: an Anthropic-shaped block list (images included) from
    `Session._finalize_tool_result`'s own return value becomes real MCP
    content blocks -- an image's `{"source": {"data", "media_type"}}`
    maps to the flatter `{"data", "mime_type"}` shape `ccbridge/__main__.
    py`'s `_content_blocks` turns into a real `mcp.types.ImageContent`."""
    if isinstance(content, list):
        blocks = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "image":
                source = b.get("source") or {}
                blocks.append({"type": "image", "data": source.get("data", ""),
                                 "mime_type": source.get("media_type") or "image/png"})
            elif isinstance(b, dict) and b.get("type") == "text":
                blocks.append({"type": "text", "text": b.get("text", "")})
            elif isinstance(b, dict):
                blocks.append({"type": "text", "text": f"[{b.get('type', 'content')} block]"})
            else:
                blocks.append({"type": "text", "text": str(b)})
        if not blocks:
            blocks = [{"type": "text", "text": ""}]
        return {"content": blocks, "is_error": bool(is_error)}
    text = content if isinstance(content, str) else str(content)
    return _wire_result(text, is_error)


def _last_assistant_text(log) -> str:
    for node in reversed(log.nodes()):
        if node.get("type") == "assistant":
            parts = [b.get("text", "") for b in (node.get("content") or [])
                     if isinstance(b, dict) and b.get("type") == "text"]
            text = "".join(parts).strip()
            if text:
                return text
    return ""


def _log_tool_use(session, tool_use_id: str, name: str, tool_input: dict) -> None:
    session.log.append_assistant(content=[{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}])


def _build_tool_context(session, tool_use_id: str) -> ToolContext:
    from halo_harness.hooks import env_file_path
    return ToolContext(
        cwd=session.cwd, read_cache=session._read_cache, abort=session.abort,
        bash_state=session._bash_state, session_dir=session.log.dir / session.log.session_id,
        registry=session.tool_registry, env=session.tool_env, catalog=session.session_catalog,
        mcp_manager=session.mcp_manager, agent_runtime=session.agent_runtime,
        job_registry=session.job_registry, vision=session.model_profile.vision,
        tool_use_id=tool_use_id,
        session_allow_rule=lambda rule_text: session.permission_engine.add_session_allow_rule(rule_text, temporary=True),
        env_file=env_file_path(session.log.session_id),
        permission_engine=session.permission_engine, effort=session.effort,
    )


def bridge_call_tool(session, name: str, arguments: dict) -> dict:
    """The bridge's `tools/call` handler -- runs SYNCHRONOUSLY on the
    bridge server's own connection thread; blocks Claude Code's own tool
    call until this returns, exactly like every other route's tool
    dispatch blocks the model from continuing (claude runs bridged tools
    serially -- finding 20's own note). Finding 21: wrapped so an
    internal crash ANYWHERE below still produces a real (is_error) MCP
    reply and a logged tool_result, never an unpaired tool_use."""
    tool_input = arguments or {}
    tool_use_id = _take_tool_use_id(session, name, tool_input)
    turn_no = session.turn_count

    _log_tool_use(session, tool_use_id, name, tool_input)
    _emit(session, events.Event("tool_use_ready", {"id": tool_use_id, "name": name, "input": tool_input,
                                                      "repaired": False}, turn=turn_no))
    state: CcState = session._cc_state
    with state.lock:
        state.in_flight.add(tool_use_id)
    try:
        return _resolve_and_dispatch_bridged_call(session, turn_no, tool_use_id, name, tool_input)
    except Exception as e:
        log.exception("bridge_call_tool: dispatch failed for %s", name)
        text = f"ccbridge: internal dispatch error: {type(e).__name__}: {e}"
        try:
            session.log.append_tool_result(tool_use_id=tool_use_id, content=text, is_error=True, tool=name,
                                             error_class="other")
        except Exception:
            pass
        _emit(session, events.Event("tool_result", {"id": tool_use_id, "ok": False, "summary": text[:200]},
                                      turn=turn_no))
        return _wire_result(text, True)
    finally:
        with state.lock:
            state.in_flight.discard(tool_use_id)


def _resolve_and_dispatch_bridged_call(session, turn_no: int, tool_use_id: str, name: str, tool_input: dict) -> dict:
    """H11b findings 3/13/17: the bridge's dispatch IS the loop's own
    dispatch -- `Session._resolve_tool_call` runs UNCHANGED (permission
    decide, PreToolUse/PermissionRequest/PermissionDenied hooks,
    EnterPlanMode/ExitPlanMode/AskUserQuestion special-casing incl.
    skipping decide() for the plan tools, the loop breaker, always-allow
    via `_apply_permission_decision`) against a trivial "already valid"
    RepairOutcome -- Claude Code's own MCP client already validated this
    call against the schema `bridge_list_tools` told it about, so there
    is nothing to repair, only to decide/dispatch. Mirrors `_dispatch_
    tools`'s own post-resolve handling exactly, minus the read-only-
    batch/agent-batch CONCURRENCY (bridged calls are already serial)."""
    from halo_harness.agent.repair import RepairOutcome
    from halo_harness.agent.subagent import run_agent_call
    from halo_harness.tools.base import ToolResult

    tu = {"id": tool_use_id, "name": name, "input": tool_input}
    outcome = RepairOutcome(block={"name": name, "input": tool_input}, ok=True)
    item = session._resolve_tool_call(tu, outcome, {})
    tool_input = item["input"]

    for msg in item.pop("hook_system_messages", None) or []:
        _emit(session, events.notification(msg))
    if "ask_reason" in item:
        _emit(session, events.Event("permission_request", {
            "id": item.get("ask_request_id", tool_use_id), "name": name, "input": tool_input,
            "reason": item["ask_reason"], "suggested_rule": item.get("suggested_rule"),
        }, turn=turn_no))
    if item.get("pending_ask"):
        # W3b item 11: same start/end/decision recording as the native
        # route's own identical call site (agent/loop.py) -- the `cc:`
        # route has its own SEPARATE copy of this blocking-ask pattern
        # (Claude Code's own MCP tool call, bridged), so it needs its own
        # copy of this instrumentation too.
        wait_start_ms = session._timeline.elapsed_ms()
        decision = session._await_permission_decision(item.get("ask_request_id", tool_use_id))
        decision_label = getattr(decision, "action", None) if decision is not None else "dismissed"
        session._timeline.record_permission_wait(wait_start_ms, session._timeline.elapsed_ms(), decision_label)
        session._apply_permission_decision(item, decision)
    if item.get("pending_question"):
        _emit(session, events.Event("question", {"id": tool_use_id, "name": name, "input": item["input"]},
                                      turn=turn_no))
        answer = session._await_reply(session._question_waiters, tool_use_id)
        item.pop("pending_question", None)
        if answer is None:
            item["text"] = "The user did not answer (the question was dismissed or the turn interrupted)."
        else:
            item["result"] = ToolResult(answer if isinstance(answer, str)
                                          else json.dumps(answer, ensure_ascii=False, default=str))
            item["ready"] = True

    special = item.get("special")
    if special == "EnterPlanMode":
        for ev in session._handle_enter_plan_mode(turn_no, item):
            _emit(session, ev)
    elif special == "ExitPlanMode":
        for ev in session._handle_exit_plan_mode(turn_no, item):
            _emit(session, ev)

    if item["ready"] and name in ("Agent", "Task"):
        if session.abort.is_set():
            item["result"] = ToolResult("Sub-agent not started: interrupted by the user.", is_error=True)
        else:
            try:
                _, tr = run_agent_call(runtime=session.agent_runtime, tool_id=tool_use_id, tool_input=item["input"],
                                         tool_name=name, on_event=lambda ev: _emit(session, ev))
                item["result"] = tr
            except Exception as e:  # run_agent_call is documented "never raises" -- defense in depth anyway
                item["result"] = ToolResult(f"Sub-agent dispatch failed: {type(e).__name__}: {e}", is_error=True)
    elif item["ready"] and "result" not in item:
        ctx = _build_tool_context(session, tool_use_id)
        item["result"] = session.tool_registry.dispatch(name, item["input"], ctx)

    session_dir = session.log.dir / session.log.session_id
    content, is_error = _drain_finalize(session, turn_no, item, session_dir)
    return _wire_result_from_content(content, is_error)


def _drain_finalize(session, turn_no: int, item: dict, session_dir) -> "tuple[object, bool]":
    """Drives `Session._finalize_tool_result` (log write + hooks + spill,
    shared verbatim with every other route) to completion, forwarding
    every event it yields, and returns its `(content, is_error)` return
    value -- see that method's own H11b docstring addition."""
    gen = session._finalize_tool_result(turn_no, item, session_dir)
    content, is_error = "", True
    while True:
        try:
            ev = next(gen)
        except StopIteration as stop:
            if isinstance(stop.value, tuple) and len(stop.value) == 2:
                content, is_error = stop.value
            return content, is_error
        _emit(session, ev)


# ---- turn execution (runs on the Session's own worker thread) -----------

def _is_replay_echo(obj: dict) -> bool:
    """Critical finding 1: `--replay-user-messages` echoes every stdin
    line back as `{"type":"user","isReplay":true,...}` once claude
    actually starts consuming it (live-verified against 2.1.284) -- the
    ONE reliable signal for "this line is no longer merely queued". The
    `tool_result` content check is belt-and-suspenders in case a future
    claude version ever reuses `isReplay` on a shape that isn't a plain
    stdin echo."""
    if obj.get("type") != "user" or not obj.get("isReplay"):
        return False
    content = (obj.get("message") or {}).get("content") or []
    return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def steer_cc(session, text: str) -> bool:
    """`Session.steer`'s cc: branch -- sends immediately (claude queues or
    absorbs it internally; `--replay-user-messages` echoes it back once
    actually consumed). Finding 1: NOT logged here -- a steer becomes a
    session-log "steer" node only once `turn_body_cc`'s reader sees that
    echo, never ahead of output it did not influence."""
    state: Optional[CcState] = getattr(session, "_cc_state", None)
    if state is None or not session.busy:
        return False
    entry = {"kind": "steer", "text": text}
    with state.lock:
        if not state.process.alive:
            return False
        state.unconfirmed.append(entry)
    try:
        state.process.send_user_line(text)
    except (BrokenPipeError, OSError):
        with state.lock:
            try:
                state.unconfirmed.remove(entry)
            except ValueError:
                pass
        return False
    _emit(session, events.notification("↳ queued for Claude Code"))
    return True


def turn_body_cc(session, turn_no: int, text: str, *, images: Optional[list] = None,
                  hook_context: Optional[str] = None):
    """The cc: equivalent of `Session._turn_body` -- hooked from
    `Session.turn()`'s single dispatch point. Always ends by yielding
    `turn_done` (same contract as every other route's turn body).
    Finding 4: `images`/`hook_context` and any pending background-job/
    sub-agent notice are sent to claude too (never JUST logged), each as
    its OWN stdin line ahead of the real turn -- every one of them was
    ALREADY logged by its own applier/by `Session.turn()` itself, so none
    of them are re-logged here, only consumed and tracked in `state.
    unconfirmed` like any other sent line."""
    try:
        state = ensure_cc_state(session)
    except ClaudeCodeUnavailable as e:
        yield events.error(str(e), turn=turn_no, err_type="cc_unavailable")
        yield events.turn_done(turn=turn_no, reason="error")
        return

    q: "queue.Queue" = queue.Queue()
    state.active_queue = q
    state.turn_is_error = False
    turn_finished = threading.Event()

    def watch_abort() -> None:
        while not turn_finished.wait(_ABORT_POLL_S):
            if session.abort.is_set():
                state.process.interrupt()
                q.put("ABORTED")

                def _cleanup() -> None:
                    if state.process.wait(timeout=_KILL_GRACE_S) is None:
                        state.process.kill()
                        state.process.wait(timeout=2.0)
                    reader_thread.join(timeout=2.0)

                cleanup_thread = threading.Thread(target=_cleanup, daemon=True, name=f"cc-cleanup-{turn_no}")
                cleanup_thread.start()
                state.cleanup_thread = cleanup_thread
                return

    def reader() -> None:
        try:
            while True:
                obj = state.process.read_event()
                if obj is None:
                    q.put("EOF")
                    return
                _confirm_session_id(session, state, obj)
                if _is_replay_echo(obj):
                    with state.tool_use_cond:
                        entry = state.unconfirmed.popleft() if state.unconfirmed else None
                    if entry is not None and entry.get("kind") == "steer":
                        session.log.append_user([{"type": "text", "text": entry["text"]}], kind="steer")
                        q.put(events.notification("Claude Code is now handling a queued message"))
                    continue
                for ev in _events_for_stdout_obj(session, turn_no, obj, state):
                    q.put(ev)
                if obj.get("type") == "result":
                    with state.lock:
                        done = not state.unconfirmed
                    if done:
                        q.put("DONE")
                        return
        except Exception as e:  # a reader crash must not hang the turn forever
            q.put(("READER_ERROR", e))

    reader_thread = threading.Thread(target=reader, daemon=True, name=f"cc-reader-{turn_no}")
    reader_thread.start()
    threading.Thread(target=watch_abort, daemon=True, name=f"cc-watch-{turn_no}").start()

    reason = "end_turn"
    try:
        try:
            context_texts = []
            for ev in session._apply_pending_job_notices(turn_no):
                if ev.kind == "user_message":
                    context_texts.append(ev.data.get("text", ""))
                yield ev
            for ev in session._apply_pending_agent_notices(turn_no):
                if ev.kind == "user_message":
                    context_texts.append(ev.data.get("text", ""))
                yield ev
            if hook_context:
                context_texts.append(hook_context)
            for ctx_text in context_texts:
                if not ctx_text:
                    continue
                with state.lock:
                    state.unconfirmed.append({"kind": "context", "text": ctx_text})
                state.process.send_user_line(ctx_text)

            blocks = [{"type": "text", "text": text}]
            for img in (images or []):
                if isinstance(img, dict):
                    blocks.append(img)
            with state.lock:
                state.unconfirmed.append({"kind": "turn", "text": text})
            state.process.send_user_blocks(blocks)
        except (BrokenPipeError, OSError) as e:
            yield events.error(f"could not send this turn to Claude Code: {e}", turn=turn_no)
            reason = "error"
            return

        while True:
            item = q.get()
            if isinstance(item, events.Event):
                yield item
                continue
            if item == "DONE":
                break
            if item == "EOF":
                # finding 8: surface WHY, not just THAT -- a resume/model/
                # auth failure claude only ever explains on stderr.
                tail = state.process.stderr_tail()
                msg = "the claude subprocess ended unexpectedly"
                if tail.strip():
                    msg += f" -- {tail.strip().splitlines()[-1][:300]}"
                yield events.error(msg, turn=turn_no, err_type="cc_eof")
                reason = "error"
                break
            if item == "ABORTED":
                deadline = time.monotonic() + _KILL_GRACE_S + 2.0
                while time.monotonic() < deadline:
                    with state.lock:
                        if not state.in_flight:
                            break
                    time.sleep(0.02)
                reason = "interrupted"
                break
            if isinstance(item, tuple) and item[0] == "READER_ERROR":
                yield events.error(f"cc: reader failed: {item[1]}", turn=turn_no)
                reason = "error"
                break
        if reason == "end_turn" and state.turn_is_error:
            reason = "error"
        if reason == "end_turn" and session.hook_runner is not None and session.hook_runner.has_hooks("Stop"):
            # must-do: Stop hooks fire on `result` -- never for an error/
            # interrupted end (matching the normal route's own "only on a
            # genuine stop" gate). `blocked` can't re-drive claude's own
            # internal loop the way it re-calls the model for every other
            # route -- the continuation is sent as one more stdin line
            # (logged + tracked exactly like a steer) for claude's NEXT
            # turn to see, rather than extending this one.
            last_text = _last_assistant_text(session.log)
            stop_outcome = session._run_hook_stop("Stop", last_assistant_message=last_text,
                                                   prompt_id=f"turn_{turn_no}", abort=session.abort)
            for msg in stop_outcome.system_messages:
                yield events.notification(msg)
            if stop_outcome.blocked:
                continuation = stop_outcome.block_reason or "Please continue."
                session.log.append_user([{"type": "text", "text": continuation}], kind="continuation")
                yield events.user_message(continuation, turn=turn_no)
                try:
                    with state.lock:
                        state.unconfirmed.append({"kind": "context", "text": continuation})
                    state.process.send_user_line(continuation)
                except (BrokenPipeError, OSError):
                    pass
    finally:
        turn_finished.set()
        state.active_queue = None
        # finding 21: a safety net for every exit path, not just Esc -- a
        # cheap no-op scan when (the common case) every bridged call
        # already logged its own result.
        from halo_harness.agent.invariants import synthesize_missing_results
        synth_reason = {"interrupted": "Tool call interrupted by user",
                          "error": "Tool call never completed (the claude subprocess ended or errored)"}.get(
            reason, "Tool call never received a result")
        synthesize_missing_results(session.log, reason=synth_reason)
    yield events.turn_done(turn=turn_no, reason=reason)


def _events_for_stdout_obj(session, turn_no: int, obj: dict, state: CcState) -> list:
    """Pure(ish) translation of one parsed stream-json line into zero or
    more UI events -- also does the corresponding session-log write, so
    this is the single place stdout-sourced content becomes both. Called
    from the reader thread; log writes are thread-safe (SessionLog's own
    lock), so no extra synchronization is needed here."""
    typ = obj.get("type")
    out: list = []
    if typ == "stream_event":
        ev = obj.get("event") or {}
        etype = ev.get("type")
        if etype == "message_start":
            state.turn_had_deltas = False
            out.append(events.message_start(turn=turn_no, model=session.model_ref.raw))
        elif etype == "content_block_delta":
            delta = ev.get("delta") or {}
            dtype = delta.get("type")
            idx = ev.get("index", 0)
            if dtype == "text_delta":
                state.turn_had_deltas = True
                out.append(events.text_delta(delta.get("text", ""), index=idx, turn=turn_no))
            elif dtype == "thinking_delta":
                # finding 19: the real field is `thinking`, not `text`
                # (verified live against 2.1.284).
                state.turn_had_deltas = True
                out.append(events.thinking_delta(delta.get("thinking", ""), index=idx, turn=turn_no))
        return out

    if typ == "system":
        # finding 12: "check init.mcp_servers" -- a bridge that failed to
        # CONNECT (never "the bridge process itself", which would just
        # make the whole claude invocation fail outright) leaves claude
        # running with zero bridged tools and, until now, no warning.
        if obj.get("subtype") == "init":
            for server in obj.get("mcp_servers") or []:
                if isinstance(server, dict) and server.get("name") == "rolo" \
                        and server.get("status") not in (None, "connected"):
                    out.append(events.notification(
                        f"cc: the tool bridge did not connect (status: {server.get('status')}) -- every "
                        f"bridged tool is unavailable this turn.", level="error"))
        return out

    if typ == "assistant":
        _record_tool_use_announcements(state, obj)
        message = obj.get("message") or {}
        blocks = message.get("content") or []
        text_like = [b for b in blocks if isinstance(b, dict) and b.get("type") != "tool_use"]
        if text_like:
            session.log.append_assistant(content=text_like)
            # finding 12: an error-shaped reply carries its text ONLY as
            # this final `assistant` message, never as stream deltas
            # (e.g. model_not_found) -- synthesize what a delta would
            # have shown so it's not silently logged with nothing ever
            # visible to the user.
            if not state.turn_had_deltas:
                for b in text_like:
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                        out.append(events.text_delta(b["text"], turn=turn_no))
                state.turn_had_deltas = True
        return out

    if typ == "result":
        usage = obj.get("usage") or {}
        cost = obj.get("total_cost_usd")
        stop_reason = obj.get("stop_reason")
        is_error = bool(obj.get("is_error"))
        subtype = obj.get("subtype")
        # finding 11: total_cost_usd is CUMULATIVE for the whole claude
        # process -- log/meter only the DELTA since the previous result
        # from THIS SAME process, never the raw cumulative figure (which
        # `CostMeter.add_usage` would otherwise add AGAIN on top of its
        # own running total every turn, compounding roughly N/2x over N
        # turns -- the exact bug verified live).
        turn_delta_cost = cost
        if isinstance(cost, (int, float)):
            if isinstance(state.last_total_cost, (int, float)):
                turn_delta_cost = max(0.0, cost - state.last_total_cost)
            state.last_total_cost = cost
        usage_for_meter = dict(usage)
        if isinstance(turn_delta_cost, (int, float)):
            usage_for_meter["cost"] = turn_delta_cost
        turn_cost = session.cost_meter.add_usage("cc", usage_for_meter)
        session.log.append_usage(usage, cost_usd=turn_delta_cost, model=session.model_ref.raw, route="cc",
                                   provider="cc", finish_reason=stop_reason,
                                   latency_ms=obj.get("duration_api_ms"),
                                   status=("error" if is_error else "ok"), estimate=True)
        out.append(events.message_end(turn=turn_no, stop_reason=stop_reason, usage=usage,
                                        cost_usd=turn_cost if turn_cost is not None else turn_delta_cost))
        if is_error:
            state.turn_is_error = True
            msg = obj.get("result") or f"claude reported an error ({subtype or 'unknown'})"
            out.append(events.error(str(msg), turn=turn_no, err_type=f"cc_{subtype or 'api_error'}"))
        with state.lock:
            # finding 20: an announcement claude made but never actually
            # called (a bare `Read` it rejected, ...) never survives past
            # its own result to steal a LATER same-name call's FIFO slot.
            state.pending_tool_uses.clear()
        out.append(session.status_event(phase="idle", turn=turn_no))
        return out

    # "user" (our own tool_result echo -- already logged by
    # bridge_call_tool; a REAL isReplay echo is intercepted before this
    # function is ever called), "rate_limit_event", anything future:
    # observed, never logged/emitted -- forward-compatible by design.
    return out

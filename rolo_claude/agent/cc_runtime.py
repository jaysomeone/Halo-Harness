"""rolo_claude.agent.cc_runtime -- H11 Part B: turn execution for a `cc:`
route Session -- lazily starts one `ClaudeCodeProcess` + `ToolBridgeServer`
pair per session, drives one turn (stream-json user line in, events out),
and runs the bridge's own "normal dispatch path" (permission decide ->
PreToolUse/PostToolUse hooks -> tool run -> session log -> UI events) for
every `mcp__rolo__<Name>` call Claude Code makes.

Threading model, per turn: the CALLING thread (the Session's own worker
thread, same as every other route) runs this module's `turn_body_cc`
generator; a background READER thread parses `claude`'s stdout stream-
json lines into `events.Event`s (queued for the generator to `yield`) and
does the session-log write for the assistant's OWN text/thinking; a
background WATCHER thread turns `session.abort` into a subprocess
interrupt; the `ToolBridgeServer`'s own connection thread(s) run
`bridge_call_tool` synchronously WHENEVER Claude Code (via the child
`ccbridge` process) actually calls a tool -- concurrently with the reader,
correlated back to Claude Code's own `toolu_...` id via a small FIFO (the
MCP `tools/call` request itself carries no id of its own; the id only
ever appears in the stream-json `assistant` tool_use announcement that
precedes it). All three feed ONE per-turn queue so the generator's own
`yield` order matches real-time arrival order regardless of which thread
produced an event.

Not used by any other route -- `derive_request` is never called here
(the model-visible context lives in Claude Code itself); this module owns
logging user/assistant/tool_result/usage nodes to `session.log` so
telemetry, `/stats`, `/export` and resume keep working exactly as they do
for every other route (brief: "rolo-claude still logs ... tool_use/
tool_result pairs, Claude Code's tool_use ids kept verbatim").
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from rolo_claude import events
from rolo_claude.agent.cc_process import (
    ClaudeCodeProcess, build_cc_argv, build_mcp_config, cc_session_uuid,
)
from rolo_claude.ccbridge.server import ToolBridgeServer
from rolo_claude.permissions import Decision
from rolo_claude.providers.cc_models import ClaudeCodeNotFoundError, claude_auth_status, resolve_claude_launch_argv
from rolo_claude.tools.base import ToolContext
from rolo_claude.tools.truncate import spill_and_truncate

_ABORT_POLL_S = 0.05
_KILL_GRACE_S = 3.0
_TOOL_USE_ID_WAIT_S = 2.0


class ClaudeCodeUnavailable(Exception):
    """`cc:` was requested but `claude` isn't installed or isn't logged
    in -- the brief's "precise one-line errors" case. Never raised for a
    transient failure (a dead subprocess mid-session is handled by
    restarting with --resume, not this)."""


@dataclass
class CcState:
    process: ClaudeCodeProcess
    bridge: ToolBridgeServer
    cc_session_id: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    pending: int = 0
    active_queue: "Optional[queue.Queue]" = None
    pending_tool_uses: list = field(default_factory=list)
    # tool_use ids `bridge_call_tool` is CURRENTLY dispatching (between
    # logging the tool_use and logging its tool_result) -- an abort must
    # wait for these to drain before synthesize_missing_results runs, or
    # it would synthesize an "interrupted" result for an id that a moment
    # later ALSO gets bridge_call_tool's own real result, violating
    # "exactly one tool_result per tool_use" in the duplicate direction.
    in_flight: set = field(default_factory=set)
    # set by a just-aborted turn's watcher thread -- the kill-then-join
    # sequence runs in the BACKGROUND (so Esc feels instant: "ABORTED" is
    # queued the moment interrupt() is sent, not after the process
    # actually dies), but `ensure_cc_state` joins this before deciding
    # whether to reuse `process` for the NEXT turn, so a next turn can
    # never race the previous turn's own orphaned reader thread for the
    # same stdout, and never mistakes "still shutting down" for "alive".
    cleanup_thread: "Optional[threading.Thread]" = None


def _preflight_cc() -> Optional[str]:
    """None when `cc:` is usable right now; else the brief's own one-line
    wording ("install Claude Code" / "run `claude` once and log in")."""
    try:
        resolve_claude_launch_argv()
    except ClaudeCodeNotFoundError:
        return ("cc: models need Claude Code installed (https://claude.com/claude-code) -- "
                 "install it, run `claude` once to log in, then try again.")
    status = claude_auth_status()
    if status is None:
        return ("cc: models need Claude Code installed (https://claude.com/claude-code) -- "
                 "install it, run `claude` once to log in, then try again.")
    if not status.logged_in:
        return "cc: models need a Claude subscription login -- run `claude` once and log in, then try again."
    return None


def ensure_cc_state(session) -> CcState:
    """Lazily start (or, after a dead epoch -- Esc/crash -- restart with
    `--resume`) this session's ONE claude subprocess + bridge server
    pair."""
    state: Optional[CcState] = getattr(session, "_cc_state", None)
    if state is not None:
        cleanup = state.cleanup_thread
        if cleanup is not None and cleanup.is_alive():
            cleanup.join(timeout=_KILL_GRACE_S + 3.0)
        if state.process.alive:
            return state
    # A restart WITHIN this process (Esc/crash killed the subprocess, or
    # `state` was never built at all here but a PRIOR process already
    # talked to claude under this same rolo session id -- `-r`/`--continue`
    # starting a brand new rolo-claude process) both need `--resume`, never
    # `--session-id` (which claude rejects once that id already exists).
    resume = state is not None or any(
        n.get("type") == "usage" and n.get("route") == "cc" for n in session.log.nodes()
    )
    err = _preflight_cc()
    if err:
        raise ClaudeCodeUnavailable(err)

    bridge = ToolBridgeServer(
        session_id=cc_session_uuid(session.log.session_id),
        list_tools_fn=lambda: bridge_list_tools(session),
        call_tool_fn=lambda name, arguments: bridge_call_tool(session, name, arguments),
    )
    bridge.start()
    mcp_config = build_mcp_config(bridge.child_env())
    argv = build_cc_argv(model=session.model_ref.model, session_id=bridge.session_id,
                          resume=resume, mcp_config=mcp_config)
    process = ClaudeCodeProcess(argv, cwd=session.cwd, env=dict(os.environ))
    new_state = CcState(process=process, bridge=bridge, cc_session_id=bridge.session_id)
    session._cc_state = new_state

    if state is None:
        pending_ctx = getattr(session, "_cc_pending_context", None)
        if pending_ctx:
            _prime_with_context(new_state, pending_ctx)
        session._cc_pending_context = None
    return new_state


def _prime_with_context(state: CcState, text: str) -> None:
    """H11 Part B: "switching into cc: mid-session sends the prior log as
    one <conversation-so-far> user message" -- sent and drained silently
    (never queued/logged as a normal turn) before the session's first
    REAL cc: turn ever runs."""
    try:
        state.process.send_user_line(text)
        while True:
            ev = state.process.read_event()
            if ev is None or ev.get("type") == "result":
                return
    except (BrokenPipeError, OSError):
        return


def prepare_conversation_so_far(session) -> None:
    """Called from `Session.set_model` when switching INTO cc: mid-session
    (provider was something else, now "cc"). Stashes the rendered prior
    transcript on the session for `ensure_cc_state`'s NEXT fresh start to
    prime the new claude process with -- documented v1 behaviour (brief)."""
    try:
        from rolo_claude.controller import render_transcript_markdown
        text = render_transcript_markdown(session.log.nodes(), session_id=session.log.session_id)
    except Exception:
        text = ""
    text = (text or "").strip()
    if text:
        session._cc_pending_context = (
            "<conversation-so-far>\n" + text + "\n</conversation-so-far>\n\n"
            "(This is context carried over from before this session switched models -- "
            "no reply needed here; just wait for the next actual message.)"
        )


def close_cc(session) -> None:
    """Kills the subprocess (process group) and closes the bridge server
    -- safe to call any number of times, including when `cc:` was never
    used this session at all."""
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


# ---- bridge tools/list + tools/call (runs on the bridge server's own
# connection thread, concurrently with turn_body_cc's reader below) -------

def bridge_list_tools(session) -> list:
    """The session's FROZEN catalog -- same defs every other route sends
    on the wire (built-ins, MCP tools, ToolSearch, Agent, plan-mode tools,
    AskUserQuestion, TodoWrite, WebFetch, BashOutput/TaskStop)."""
    if session.session_catalog is not None:
        return session.tool_registry.definitions_for(session.session_catalog.names)
    return session.tool_registry.definitions()


def _emit(session, ev: "events.Event") -> None:
    state: Optional[CcState] = getattr(session, "_cc_state", None)
    q = state.active_queue if state is not None else None
    if q is not None:
        q.put(ev)


def _take_tool_use_id(session, name: str) -> str:
    """Correlates a `tools/call` (name+arguments only) back to Claude
    Code's OWN `toolu_...` id, announced earlier on the stream-json
    channel (see module docstring) -- FIFO by name, bounded wait for the
    reader thread to catch up, never blocks forever; a synthetic id is
    used as a last resort so a call is never dropped."""
    import uuid as _uuid
    state: CcState = session._cc_state
    deadline = time.monotonic() + _TOOL_USE_ID_WAIT_S
    while True:
        with state.lock:
            for i, pending in enumerate(state.pending_tool_uses):
                if pending.get("name") == name:
                    del state.pending_tool_uses[i]
                    return pending.get("id") or f"ccbridge_{_uuid.uuid4().hex[:12]}"
        if time.monotonic() >= deadline:
            return f"ccbridge_{_uuid.uuid4().hex[:12]}"
        time.sleep(0.02)


def _tool_result_content_for_log(tool, tr, *, session, tool_use_id: str):
    if isinstance(tr.content, list):
        parts = []
        for b in tr.content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, dict):
                parts.append(f"[{b.get('type', 'content')} block]")
            else:
                parts.append(str(b))
        text = "\n".join(parts)
    else:
        text = tr.content if isinstance(tr.content, str) else str(tr.content)
    session_dir = session.log.dir / session.log.session_id
    return spill_and_truncate(text, cap=session.tool_registry.result_cap(tool.name if tool else ""),
                               session_dir=session_dir, tool_use_id=tool_use_id)


def _wire_result(text: str, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": bool(is_error)}


def _log_tool_use(session, tool_use_id: str, name: str, tool_input: dict) -> None:
    session.log.append_assistant(content=[{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}])


def _finish_denied(session, tool_use_id: str, name: str, tool_input: dict, reason: str) -> dict:
    session.log.append_tool_result(tool_use_id=tool_use_id, content=reason, is_error=True,
                                    tool=name, error_class="denied_by_rule")
    _emit(session, events.Event("tool_result", {"id": tool_use_id, "ok": False, "summary": reason[:200]},
                                  turn=session.turn_count))
    session.permission_denials.append({"tool_name": name, "tool_input": tool_input, "reason": reason})
    return _wire_result(reason, True)


def bridge_call_tool(session, name: str, arguments: dict) -> dict:
    """The bridge's `tools/call` handler -- the "normal dispatch path"
    (brief): permission decide -> PreToolUse/PostToolUse hooks -> tool
    run -> session log -> UI events. Runs SYNCHRONOUSLY on the bridge
    server's own connection thread; blocks Claude Code's own tool call
    until this returns, exactly like every other route's tool dispatch
    blocks the model from continuing."""
    tool_input = arguments or {}
    tool_use_id = _take_tool_use_id(session, name)
    tool = session.tool_registry.get(name)

    _log_tool_use(session, tool_use_id, name, tool_input)
    _emit(session, events.Event("tool_use_ready", {"id": tool_use_id, "name": name, "input": tool_input,
                                                      "repaired": False}, turn=session.turn_count))
    state: CcState = session._cc_state
    with state.lock:
        state.in_flight.add(tool_use_id)
    try:
        return _dispatch_after_tool_use_logged(session, name, tool_input, tool, tool_use_id)
    finally:
        with state.lock:
            state.in_flight.discard(tool_use_id)


def _dispatch_after_tool_use_logged(session, name: str, tool_input: dict, tool, tool_use_id: str) -> dict:
    if tool is None:
        return _finish_denied(session, tool_use_id, name, tool_input, f"Unknown tool: {name!r}")

    decision: Decision = session.permission_engine.decide(name, tool_input, tool=tool)

    if session.hook_runner is not None and session.hook_runner.has_hooks("PreToolUse"):
        payload = session.hook_runner.payload(
            "PreToolUse", prompt_id=f"turn_{session.turn_count}",
            extra={"tool_name": name, "tool_input": tool_input, "tool_use_id": tool_use_id},
        )
        pre = session.hook_runner.run("PreToolUse", payload, matched=name, tool_name=name,
                                        tool_input=tool_input, tool=tool, abort=session.abort)
        for msg in pre.system_messages:
            _emit(session, events.notification(msg))
        if pre.updated_input is not None:
            tool_input = pre.updated_input
            decision = session.permission_engine.decide(name, tool_input, tool=tool)
        if pre.blocked:
            return _finish_denied(session, tool_use_id, name, tool_input,
                                    f"Blocked by a PreToolUse hook: {pre.block_reason or 'blocked'}")
        if pre.permission_decision and decision.action != "deny" and pre.permission_decision in ("allow", "deny", "ask"):
            reason = pre.permission_decision_reason or f"{pre.permission_decision}ed by a PreToolUse hook"
            decision = Decision(pre.permission_decision, reason, source="hook")

    if decision.action == "deny":
        return _finish_denied(session, tool_use_id, name, tool_input, decision.reason)

    if decision.action == "ask":
        if not session.interactive:
            return _finish_denied(
                session, tool_use_id, name, tool_input,
                f"{name!r} needs permission but no UI is attached to ask "
                f"(suggested rule: {decision.suggested_rule})",
            )
        request_id = tool_use_id
        suffix = 2
        while request_id in session._permission_waiters:
            request_id = f"{tool_use_id}#{suffix}"
            suffix += 1
        session._permission_waiters[request_id] = {"event": threading.Event(), "decision": None}
        _emit(session, events.Event("permission_request", {"id": request_id, "name": name, "input": tool_input,
                                                              "reason": decision.reason,
                                                              "suggested_rule": decision.suggested_rule},
                                      turn=session.turn_count))
        reply = session._await_permission_decision(request_id)
        allowed = isinstance(reply, Decision) and reply.action == "allow"
        if not allowed:
            reason = reply.reason if isinstance(reply, Decision) else "denied (no answer -- interrupted)"
            return _finish_denied(session, tool_use_id, name, tool_input, reason)

    ctx = _build_tool_context(session, tool_use_id)
    tr = session.tool_registry.dispatch(name, tool_input, ctx)
    tr, hook_msgs = session._apply_post_tool_use_hooks(name, tool_use_id, tool_input, tr)
    for msg in hook_msgs:
        _emit(session, events.notification(msg))

    content_for_log = _tool_result_content_for_log(tool, tr, session=session, tool_use_id=tool_use_id)
    error_class = None
    if tr.is_error:
        try:
            from rolo_claude.agent.loop import _classify_tool_error_text
            error_class = _classify_tool_error_text(name, content_for_log)
        except Exception:
            error_class = "other"
    session.log.append_tool_result(tool_use_id=tool_use_id, content=content_for_log, is_error=tr.is_error,
                                    tool=name, error_class=error_class, ms=tr.duration_ms,
                                    num_bytes=len(content_for_log.encode("utf-8", "replace")))
    _emit(session, events.Event("tool_result", {"id": tool_use_id, "ok": not tr.is_error,
                                                   "summary": content_for_log[:200], "content": content_for_log},
                                  turn=session.turn_count))
    return _wire_result(content_for_log, tr.is_error)


def _build_tool_context(session, tool_use_id: str) -> ToolContext:
    from rolo_claude.hooks import env_file_path
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


# ---- turn execution (runs on the Session's own worker thread) -----------

def steer_cc(session, text: str) -> bool:
    """`Session.steer`'s cc: branch -- sends immediately (Claude Code
    queues it internally; `queued_turn_count` in its own result JSON
    confirms this natively-supported behaviour), rather than the default
    route's "apply at the next safe point inside THIS turn" queue."""
    state: Optional[CcState] = getattr(session, "_cc_state", None)
    if state is None or not session.busy:
        return False
    with state.lock:
        if not state.process.alive:
            return False
        state.pending += 1
    try:
        state.process.send_user_line(text)
    except (BrokenPipeError, OSError):
        with state.lock:
            state.pending -= 1
        return False
    session.log.append_user([{"type": "text", "text": text}], kind="steer")
    _emit(session, events.notification("↳ queued for Claude Code"))
    return True


def turn_body_cc(session, turn_no: int, text: str):
    """The cc: equivalent of `Session._turn_body` -- hooked from
    `Session.turn()`'s single dispatch point. Always ends by yielding
    `turn_done` (same contract as every other route's turn body)."""
    try:
        state = ensure_cc_state(session)
    except ClaudeCodeUnavailable as e:
        yield events.error(str(e), turn=turn_no, err_type="cc_unavailable")
        yield events.turn_done(turn=turn_no, reason="error")
        return

    q: "queue.Queue" = queue.Queue()
    state.active_queue = q
    with state.lock:
        state.pending += 1
    turn_finished = threading.Event()

    def watch_abort() -> None:
        while not turn_finished.wait(_ABORT_POLL_S):
            if session.abort.is_set():
                state.process.interrupt()
                # "ABORTED" is queued THE INSTANT the signal is sent (Esc
                # must feel instant) -- the actual kill-if-still-alive +
                # reader-thread-join cleanup runs in the background;
                # ensure_cc_state joins THIS thread before the next turn
                # ever reuses (or wrongly believes still-alive) `process`.
                q.put("ABORTED")

                def _cleanup() -> None:
                    if state.process.wait(timeout=_KILL_GRACE_S) is None:
                        state.process.kill()
                        state.process.wait(timeout=2.0)
                    reader_thread.join(timeout=2.0)

                cleanup_thread = threading.Thread(target=_cleanup, daemon=True, name=f"cc-cleanup-{turn_no}")
                # start() BEFORE publishing to state.cleanup_thread -- a
                # reader on another thread (ensure_cc_state, or a test)
                # must never observe this attribute set to a Thread that
                # isn't started yet (Thread.join() raises RuntimeError on
                # one that hasn't been start()-ed).
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
                for ev in _events_for_stdout_obj(session, turn_no, obj, state):
                    q.put(ev)
                if obj.get("type") == "result":
                    with state.lock:
                        state.pending -= 1
                        done = state.pending <= 0
                    if done:
                        q.put("DONE")
                        return
        except Exception as e:  # a reader crash must not hang the turn forever
            q.put(("READER_ERROR", e))

    # `reader_thread` must exist in this closure's enclosing scope BEFORE
    # `watch_abort` ever starts running (its own `_cleanup` closure reads
    # it) -- started first, THEN the watcher, never the reverse.
    reader_thread = threading.Thread(target=reader, daemon=True, name=f"cc-reader-{turn_no}")
    reader_thread.start()
    threading.Thread(target=watch_abort, daemon=True, name=f"cc-watch-{turn_no}").start()

    try:
        state.process.send_user_line(text)
    except (BrokenPipeError, OSError) as e:
        yield events.error(f"could not send this turn to Claude Code: {e}", turn=turn_no)
        yield events.turn_done(turn=turn_no, reason="error")
        return

    reason = "end_turn"
    try:
        while True:
            item = q.get()
            if isinstance(item, events.Event):
                yield item
                continue
            if item == "DONE":
                break
            if item == "EOF":
                yield events.error("the claude subprocess ended unexpectedly", turn=turn_no, err_type="cc_eof")
                reason = "error"
                break
            if item == "ABORTED":
                # bridge_call_tool's own dispatch (permission/hooks/tool
                # run -- ctx.abort=session.abort, so a real Bash etc.
                # notices and returns promptly) is left to actually FINISH
                # and log its own real tool_result; waiting (bounded) for
                # `state.in_flight` to drain first means
                # synthesize_missing_results below only ever sees a
                # tool_use that GENUINELY never got a result (e.g. one
                # Claude Code announced but never actually called before
                # dying), never one a moment away from its own real
                # answer -- which would otherwise log TWO tool_results for
                # the same id (the invariant's OTHER failure mode).
                deadline = time.monotonic() + _KILL_GRACE_S + 2.0
                while time.monotonic() < deadline:
                    with state.lock:
                        if not state.in_flight:
                            break
                    time.sleep(0.02)
                from rolo_claude.agent.invariants import synthesize_missing_results
                synthesize_missing_results(session.log, reason="Tool call interrupted by user")
                reason = "interrupted"
                break
            if isinstance(item, tuple) and item[0] == "READER_ERROR":
                yield events.error(f"cc: reader failed: {item[1]}", turn=turn_no)
                reason = "error"
                break
    finally:
        turn_finished.set()
        state.active_queue = None
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
            out.append(events.message_start(turn=turn_no, model=session.model_ref.raw))
        elif etype == "content_block_delta":
            delta = ev.get("delta") or {}
            dtype = delta.get("type")
            idx = ev.get("index", 0)
            if dtype == "text_delta":
                out.append(events.text_delta(delta.get("text", ""), index=idx, turn=turn_no))
            elif dtype == "thinking_delta":
                out.append(events.thinking_delta(delta.get("text", ""), index=idx, turn=turn_no))
        return out

    if typ == "assistant":
        from rolo_claude.mcp.manager import split_mcp_tool_name

        message = obj.get("message") or {}
        blocks = message.get("content") or []
        text_like = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                # Claude Code announces the WIRE name (mcp__rolo__<Name>,
                # bin sec.9's naming) here, but the actual downstream MCP
                # tools/call -- what bridge_call_tool's own `name` param
                # is -- always uses the BARE name (the prefix is Claude
                # Code's own model-facing convention, stripped again
                # before it ever reaches our ccbridge child) -- stored
                # bare so `_take_tool_use_id`'s FIFO match succeeds.
                raw_name = b.get("name") or ""
                split = split_mcp_tool_name(raw_name)
                bare_name = split[1] if split else raw_name
                with state.lock:
                    state.pending_tool_uses.append(
                        {"id": b.get("id"), "name": bare_name, "input": b.get("input") or {}}
                    )
            elif isinstance(b, dict):
                text_like.append(b)
        if text_like:
            session.log.append_assistant(content=text_like)
        return out

    if typ == "result":
        usage = obj.get("usage") or {}
        cost = obj.get("total_cost_usd")
        stop_reason = obj.get("stop_reason")
        usage_for_meter = dict(usage)
        if isinstance(cost, (int, float)):
            usage_for_meter["cost"] = cost
        turn_cost = session.cost_meter.add_usage("cc", usage_for_meter)
        session.log.append_usage(usage, cost_usd=cost, model=session.model_ref.raw, route="cc",
                                   provider="cc", finish_reason=stop_reason,
                                   latency_ms=obj.get("duration_api_ms"), status="ok", estimate=True)
        out.append(events.message_end(turn=turn_no, stop_reason=stop_reason, usage=usage,
                                        cost_usd=turn_cost if turn_cost is not None else cost))
        out.append(session.status_event(phase="idle", turn=turn_no))
        return out

    # "system" (init/status), "user" (our own tool_result echo -- already
    # logged by bridge_call_tool), "rate_limit_event", anything future:
    # observed, never logged/emitted -- forward-compatible by design.
    return out

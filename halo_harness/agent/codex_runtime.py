"""halo_harness.agent.codex_runtime -- Halo 2.0.3 round 5i part 2: session-
level state + the `mcp_servers.halo` tool bridge for the `cx:` route.
Mirrors `agent.cc_runtime`'s shape where Codex's architecture matches
Claude Code's (the bridge dispatch: permission decide, hooks, Agent/Task,
finalize -- `_resolve_and_dispatch_bridged_call` below is a close copy of
`cc_runtime`'s own, adapted to this module's `CxState` instead of `CcState`)
and deliberately diverges where it does not (see module docstring of
`agent/codex_turn.py`, which owns actual turn execution, for why there is
no long-held subprocess here). docs/harness/CODEX-RESEARCH.md is the record
of what's confirmed vs assumed.

Four small pure helpers (`_build_tool_context`, `_log_tool_use`,
`_wire_result`, `_wire_result_from_content`) are imported from `cc_runtime`
rather than duplicated -- they only ever read `session.*`/plain dicts, never
`session._cc_state`, so they are genuinely provider-neutral; everything
that DOES touch per-provider state (`_emit`, the dispatch, finalize) has
its own copy here keyed on `session._cx_state` instead, rather than
refactoring `cc_runtime`'s heavily-tested internals to take the state as a
parameter.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid as _uuid_mod
from dataclasses import dataclass, field
from typing import Optional

from halo_harness import events
from halo_harness.agent.cc_runtime import _build_tool_context, _log_tool_use, _wire_result, _wire_result_from_content
from halo_harness.ccbridge.server import ToolBridgeServer
from halo_harness.providers.codex_models import CodexNotFoundError, codex_login_status, resolve_codex_launch_argv

log = logging.getLogger("bridge")

_TOOL_USE_ID_WAIT_S = 5.0


class CodexUnavailable(Exception):
    """`cx:` was requested but `codex` isn't installed or isn't logged in
    with a ChatGPT subscription -- never raised for a transient per-turn
    failure (that's `codex_turn.turn_body_cx`'s own EOF/stderr handling)."""


@dataclass
class CxState:
    bridge: ToolBridgeServer
    lock: threading.Lock = field(default_factory=threading.Lock)
    cx_session_id: Optional[str] = None
    active_queue: "Optional[object]" = None
    pending_tool_uses: list = field(default_factory=list)
    tool_use_cond: "Optional[threading.Condition]" = None
    in_flight: set = field(default_factory=set)
    # codex_turn.steer_cx appends here; turn_body_cx drains it as soon as
    # the current turn's subprocess exits (CODEX-RESEARCH.md section 7's
    # documented "finish the turn, then send" fallback).
    pending_steer_texts: list = field(default_factory=list)
    preamble_sent: bool = False
    # codex_turn._events_for_cx_obj's own incremental-text bookkeeping: how
    # many chars of each live `agent_message`/`reasoning` item's text have
    # already been emitted as a delta, keyed by item id -- reset per
    # subprocess call is unnecessary (item ids are UUIDs, never reused).
    cx_text_lens: dict = field(default_factory=dict)
    cx_thinking_lens: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.tool_use_cond = threading.Condition(self.lock)


def _preflight_cx() -> Optional[str]:
    """None when `cx:` is usable right now; else a precise one-line
    reason -- mirrors `cc_runtime._preflight_cc` exactly, substituting the
    text-based `codex login status` check (CODEX-RESEARCH.md section 1)
    for `claude auth status`'s JSON."""
    try:
        resolve_codex_launch_argv()
    except CodexNotFoundError:
        return ("cx: models need Codex CLI installed (https://developers.openai.com/codex) -- "
                 "install it, run `codex login` once, then try again.")
    status = codex_login_status()
    if status is None:
        return ("cx: models need Codex CLI installed (https://developers.openai.com/codex) -- "
                 "install it, run `codex login` once, then try again.")
    if getattr(status, "timed_out", False):
        return "cx: could not check `codex login status` (it timed out) -- check the codex binary and try again."
    if not status.logged_in:
        return "cx: models need a ChatGPT subscription login -- run `codex login` once, then try again."
    if status.auth_method != "chatgpt":
        return (f"cx: would not use the subscription -- `codex login status` reports {status.auth_method!r}, "
                f"not a ChatGPT login (an OPENAI_API_KEY login belongs on the oai: route instead: use "
                f"oai:<model>). Run `codex login` and sign in with ChatGPT for cx:.")
    return None


def _cx_child_env(session) -> dict:
    from halo_harness.providers.config import cc_child_env
    base = getattr(session, "tool_env", None) or dict(os.environ)
    return cc_child_env(base)


def _last_cx_session_id(log) -> Optional[str]:
    """The codex thread id (Codex's OWN, learned from a `thread.started`
    event and logged as a `cx_session_id` meta node) to `resume` -- `/clear`
    starts a fresh log with none, so a freshly-created `CxState` after that
    has no id to resume either, same as `cc_runtime._last_cc_session_id`."""
    for node in reversed(log.nodes()):
        if node.get("type") == "meta" and node.get("cx_session_id"):
            return node["cx_session_id"]
    return None


def ensure_cx_state(session) -> CxState:
    """Lazily creates this session's ONE bridge server (never a persistent
    `codex` process -- each turn gets its own, see `codex_turn.py`). Reused
    across every `cx:` turn in the session; torn down by `close_cx`. Seeds
    `CxState.cx_session_id` from the log's own last-known thread id (e.g. a
    `--resume`d halo session, or a cx:->other->cx: model round trip that
    already closed the bridge once) so the NEXT turn resumes Codex's own
    thread instead of silently starting a new one."""
    state: Optional[CxState] = getattr(session, "_cx_state", None)
    if state is not None:
        return state
    err = _preflight_cx()
    if err:
        raise CodexUnavailable(err)
    bridge = ToolBridgeServer(session_id=str(_uuid_mod.uuid4()), list_tools_fn=lambda: bridge_list_tools(session),
                               call_tool_fn=lambda name, arguments: bridge_call_tool(session, name, arguments))
    bridge.start()
    state = CxState(bridge=bridge, cx_session_id=_last_cx_session_id(session.log))
    session._cx_state = state
    return state


def close_cx(session) -> None:
    """Safe to call any number of times, including when `cx:` was never
    used this session. Called from `Session.close_cc` (broadened to tear
    down BOTH `_cc_state` and `_cx_state`, see agent/loop.py) rather than
    adding a parallel `close_cx()` call at every one of that method's own
    ~8 call sites. Walks `agent_runtime.live_children` the SAME way
    `cc_runtime.close_cc` does -- that walk already runs unconditionally
    from the `Session.close_cc` wrapper, but it only ever recurses into
    ITSELF (`cc_runtime.close_cc(child)`), which is a no-op for a child
    whose own `_cx_state` (not `_cc_state`) is what's actually live; this
    is that same walk's `cx:` counterpart, so a `cx:` sub-agent's bridge
    is never left orphaned either."""
    runtime = getattr(session, "agent_runtime", None)
    if runtime is not None:
        for child in list(getattr(runtime, "live_children", {}).values()):
            close_cx(child)
    state: Optional[CxState] = getattr(session, "_cx_state", None)
    if state is None:
        return
    try:
        state.bridge.close()
    except Exception:
        pass
    session._cx_state = None


def bridge_list_tools(session) -> list:
    """Identical contract to `cc_runtime.bridge_list_tools` -- the
    session's current frozen catalog, same annotations/_meta passthrough."""
    from halo_harness.agent.cc_runtime import bridge_list_tools as _cc_bridge_list_tools
    return _cc_bridge_list_tools(session)


def _emit(session, ev: "events.Event") -> None:
    state: Optional[CxState] = getattr(session, "_cx_state", None)
    q = state.active_queue if state is not None else None
    if q is not None:
        q.put(ev)


def _take_tool_use_id(session, name: str, tool_input: dict) -> str:
    """Correlates a bridge `tools/call` back to an announced `mcp_tool_
    call` item id -- same (name, input)-matching reasoning `cc_runtime.
    _take_tool_use_id` documents (never name-only: an announced-but-never-
    called item must not steal a later same-name call's id), adapted to
    `session._cx_state`. A synthetic id is the last resort."""
    import time
    state: CxState = session._cx_state
    deadline = time.monotonic() + _TOOL_USE_ID_WAIT_S
    with state.tool_use_cond:
        while True:
            for i, pending in enumerate(state.pending_tool_uses):
                if pending.get("name") == name and pending.get("input") == (tool_input or {}):
                    del state.pending_tool_uses[i]
                    return pending.get("id") or f"cxbridge_{_uuid_mod.uuid4().hex[:12]}"
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not state.tool_use_cond.wait(timeout=min(remaining, 0.25)):
                if time.monotonic() >= deadline:
                    for i, pending in enumerate(state.pending_tool_uses):
                        if pending.get("name") == name:
                            del state.pending_tool_uses[i]
                            return pending.get("id") or f"cxbridge_{_uuid_mod.uuid4().hex[:12]}"
                    return f"cxbridge_{_uuid_mod.uuid4().hex[:12]}"


def bridge_call_tool(session, name: str, arguments: dict) -> dict:
    """The bridge's `tools/call` handler -- synchronous on the bridge
    server's own connection thread, exactly like `cc_runtime.bridge_call_
    tool`; wrapped so an internal crash still produces a real (is_error)
    reply and a logged tool_result, never an unpaired tool_use."""
    tool_input = arguments or {}
    tool_use_id = _take_tool_use_id(session, name, tool_input)
    turn_no = session.turn_count
    _log_tool_use(session, tool_use_id, name, tool_input)
    _emit(session, events.Event("tool_use_ready", {"id": tool_use_id, "name": name, "input": tool_input,
                                                      "repaired": False}, turn=turn_no))
    state: CxState = session._cx_state
    with state.lock:
        state.in_flight.add(tool_use_id)
    try:
        return _resolve_and_dispatch_bridged_call(session, turn_no, tool_use_id, name, tool_input)
    except Exception as e:
        log.exception("codex bridge_call_tool: dispatch failed for %s", name)
        text = f"cxbridge: internal dispatch error: {type(e).__name__}: {e}"
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


def record_tool_use_announcement(session, *, tool_use_id: str, name: str, tool_input: dict) -> None:
    """Called from `codex_turn.py` when an `item.started`/`item.completed`
    event of type `mcp_tool_call` names a call that's about to (or already
    did) land on the bridge -- lets `_take_tool_use_id` correlate the
    REAL bridge `tools/call` back to Codex's own display id, exactly like
    `cc_runtime._record_tool_use_announcements` does for claude's
    `tool_use` blocks."""
    state: Optional[CxState] = getattr(session, "_cx_state", None)
    if state is None:
        return
    with state.lock:
        state.pending_tool_uses.append({"id": tool_use_id, "name": name, "input": tool_input or {}})
        state.tool_use_cond.notify_all()


def _resolve_and_dispatch_bridged_call(session, turn_no: int, tool_use_id: str, name: str, tool_input: dict) -> dict:
    """Adapted from `cc_runtime._resolve_and_dispatch_bridged_call` --
    same reasoning: Codex's own MCP client already validated this call
    against the schema `bridge_list_tools` told it about, so there is
    nothing to repair, only to decide/dispatch through the SAME permission
    engine/hooks/Agent-Task/finalize path every other route's tool
    dispatch uses."""
    from halo_harness.agent.repair import RepairOutcome
    from halo_harness.agent.subagent import run_agent_call
    from halo_harness.tools.base import ToolResult
    import json as json_mod

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
        wait_start_ms = session._timeline.elapsed_ms()
        decision = session._await_permission_decision(item.get("ask_request_id", tool_use_id))
        decision_label = getattr(decision, "action", None) if decision is not None else "dismissed"
        session._timeline.record_permission_wait(wait_start_ms, session._timeline.elapsed_ms(), decision_label)
        session._apply_permission_decision(item, decision)
    if item.get("pending_question"):
        question_request_id = item.get("question_request_id", tool_use_id)
        _emit(session, events.Event("question", {"id": question_request_id, "name": name, "input": item["input"]},
                                      turn=turn_no))
        answer = session._await_reply(session._question_waiters, question_request_id)
        item.pop("pending_question", None)
        if answer is None:
            item["text"] = "The user did not answer (the question was dismissed or the turn interrupted)."
        else:
            item["result"] = ToolResult(answer if isinstance(answer, str)
                                          else json_mod.dumps(answer, ensure_ascii=False, default=str))
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
            except Exception as e:
                item["result"] = ToolResult(f"Sub-agent dispatch failed: {type(e).__name__}: {e}", is_error=True)
    elif item["ready"] and "result" not in item:
        ctx = _build_tool_context(session, tool_use_id)
        item["result"] = session.tool_registry.dispatch(name, item["input"], ctx)

    session_dir = session.log.dir / session.log.session_id
    content, is_error = _drain_finalize(session, turn_no, item, session_dir)
    return _wire_result_from_content(content, is_error)


def _drain_finalize(session, turn_no: int, item: dict, session_dir) -> "tuple[object, bool]":
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


def one_shot_cx_call(model: str, system_text: str, user_text: str, *, timeout_s: float = 60.0) -> str:
    """A quick, STATELESS `codex exec --ephemeral` call for `Session.
    call_small_model`'s cx: branch -- mirrors `cc_runtime.one_shot_cc_call`.
    Never touches any session's own live `_cx_state`. Raises RuntimeError
    on failure (the same contract every `call_small_model` branch has)."""
    import subprocess as subprocess_mod
    from halo_harness.agent.codex_process import build_cx_argv
    from halo_harness.providers.codex_models import CodexNotFoundError
    try:
        prompt = f"{system_text}\n\n{user_text}"
        argv = build_cx_argv(model=model, prompt=prompt, resume_id=None, permission_mode="bypass",
                               mcp_override_args=[], ephemeral=True)
    except CodexNotFoundError as e:
        raise RuntimeError(f"cx: small-model call unavailable: {e}") from e
    from halo_harness.providers.config import cc_child_env
    env = cc_child_env(dict(os.environ))
    try:
        proc = subprocess_mod.run(argv, capture_output=True, text=True, timeout=timeout_s, env=env)
    except subprocess_mod.TimeoutExpired as e:
        raise RuntimeError(f"cx: small-model call timed out after {timeout_s}s") from e
    except OSError as e:
        raise RuntimeError(f"cx: small-model call failed to start: {e}") from e
    from halo_harness.agent.codex_turn import extract_final_text_from_jsonl
    text = extract_final_text_from_jsonl(proc.stdout)
    if text is None:
        raise RuntimeError(f"cx: small-model call produced no parseable output (exit {proc.returncode})")
    return text

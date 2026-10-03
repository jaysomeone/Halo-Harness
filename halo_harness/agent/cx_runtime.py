"""halo_harness.agent.cx_runtime -- turn execution for a `cx:` route
session: the installed `codex` binary is the model, spending the user's
ChatGPT subscription (Plus, Pro, Team...) instead of an API key. The Codex
counterpart of `agent/cc_runtime.py`.

Shape: one `codex app-server` process (agent/cx_process.py) plus one
`ToolBridgeServer` (ccbridge, shared with `cc:`) per session. Codex gets
Halo's tools through an MCP server named "halo" (`mcp__halo__<Name>`)
declared in the thread's own config; every call is dispatched by the same
`cc_runtime.resolve_bridged_call` path the `cc:` route uses, so permission
rules, hooks, plan mode, AskUserQuestion and Agent behave exactly as on
every other route. Codex's own shell, browser, image and plugin tools are
switched off. Codex keeps `apply_patch` on the models that carry it; the
thread runs with approvals on and a read-only sandbox, so each native file
change arrives as an approval request and Halo decides it as a Write/Edit
call (rules, hooks and the dock included) before Codex may apply it.

Conversation state lives in Codex's own thread (`thread/start`, then
`thread/resume <id>` after a restart or a resumed Halo session); the id is
kept in the session log as a `cx_thread_id` meta node. Steering uses
`turn/steer`, Esc uses `turn/interrupt`, and the account's usage windows
arrive with every response (`account/rateLimits/updated`).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid as _uuid_mod
from dataclasses import dataclass, field
from typing import Optional

from halo_harness import events
from halo_harness.agent.cx_process import CodexAppServer, CodexRpcError
from halo_harness.ccbridge.server import ToolBridgeServer
from halo_harness.providers.cx_models import CodexNotFoundError

log = logging.getLogger("bridge")

BRIDGE_SERVER_NAME = "halo"
_INTERRUPT_GRACE_S = 5.0
_TOOL_USE_ID_WAIT_S = 5.0
_INSTRUCTIONS_CHARS = 16_000
_MCP_TOOL_TIMEOUT_S = 7 * 24 * 3600   # a bridged call may wait on the user; never let Codex time it out

# Codex features that would give the model tools outside Halo's engine.
# Passed as `-c features.<name>=false`: a name an older or newer codex
# does not know is ignored, never an error.
_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "view_image", "apps", "browser_use", "browser_use_external", "computer_use",
    "image_generation", "multi_agent", "multi_agent_v2", "sleep_tool", "tool_suggest", "in_app_browser",
    "plugins", "hooks", "goals", "skill_search", "memories", "standalone_web_search",
)

CX_DEVELOPER_INSTRUCTIONS = (
    "You are running inside Halo, a harness that provides your tools through one MCP server named "
    "\"halo\" (tools appear as mcp__halo__<Name>): Read, Write, Edit, Bash, Glob, Grep, WebFetch, "
    "TodoWrite, Agent, AskUserQuestion and more. Use them for every file read, file change, command, "
    "web fetch and question to the user -- prefer mcp__halo__Edit and mcp__halo__Write over apply_patch, "
    "and mcp__halo__AskUserQuestion over request_user_input. Halo applies the user's permission rules, "
    "hooks and plan mode to these tools; a denied call comes back with the reason."
)


class CodexUnavailable(Exception):
    """`cx:` was requested but codex is missing or not logged in to ChatGPT."""


@dataclass
class CxState:
    server: CodexAppServer
    bridge: ToolBridgeServer
    thread_id: str
    model: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    cond: "Optional[threading.Condition]" = None
    active_queue: "Optional[queue.Queue]" = None
    turn_no: int = 0
    turn_id: Optional[str] = None
    pending_tool_uses: list = field(default_factory=list)   # announced mcpToolCall items
    in_flight: set = field(default_factory=set)
    native_items: dict = field(default_factory=dict)         # itemId -> {"tool_use_id","name","input"}
    file_changes: dict = field(default_factory=dict)         # itemId -> changes, from item/started
    delta_items: set = field(default_factory=set)            # agentMessage item ids that streamed deltas
    turn_usage: dict = field(default_factory=dict)            # summed `last` usage of this turn's responses
    turn_error: Optional[str] = None
    pending_context: list = field(default_factory=list)      # texts to send ahead of the next turn

    def __post_init__(self) -> None:
        self.cond = threading.Condition(self.lock)


# One informational line per process when the installed codex is newer
# than cx_tested.json (doctor has the always-on version of this check).
_version_notice_emitted = False


def _maybe_warn_version_outside_tested_range() -> None:
    global _version_notice_emitted
    if _version_notice_emitted:
        return
    _version_notice_emitted = True
    try:
        from halo_harness.providers.cx_models import (
            codex_version_outside_tested_range, installed_codex_version, load_cx_tested_range,
        )
        version, tested = installed_codex_version(), load_cx_tested_range()
        if codex_version_outside_tested_range(version, tested=tested):
            log.warning("cx: installed codex %s is newer than the tested range (%s-%s, verified %s) -- "
                        "watch for behavior changes", version, tested.get("min"), tested.get("max"), tested.get("date"))
    except Exception:
        pass


def preflight_cx() -> Optional[str]:
    """None when `cx:` is usable now; else one line saying what to do."""
    from halo_harness.providers.cx_models import codex_installed, refresh_cached_codex_login_status
    if not codex_installed():
        return ("cx: models need the Codex CLI installed (`npm install -g @openai/codex`), then "
                "`codex login` with your ChatGPT account.")
    status = refresh_cached_codex_login_status()
    if status is None:
        return "cx: could not run `codex login status` -- check the codex binary and try again."
    if status.timed_out:
        return "cx: `codex login status` timed out -- check the codex binary and try again."
    if not status.logged_in:
        return "cx: models need a ChatGPT login -- run `codex login` and choose Sign in with ChatGPT."
    if status.method != "chatgpt":
        return ("cx: codex is logged in with an API key, not a ChatGPT subscription -- run `codex logout`, "
                "then `codex login` and choose Sign in with ChatGPT.")
    return None


def _child_env(session) -> dict:
    from halo_harness.providers.cx_models import cx_child_env
    return cx_child_env(getattr(session, "tool_env", None) or dict(os.environ))


def _server_args() -> list:
    args = []
    for name in _DISABLED_FEATURES:
        args += ["-c", f"features.{name}=false"]
    args += ["-c", 'web_search="disabled"']
    return args


def _bridge_mcp_entry(bridge: ToolBridgeServer) -> dict:
    """The `mcp_servers.halo` entry. Codex hands a stdio MCP child only the
    env it is configured with, so the bridge's socket details go here (in
    the JSON-RPC thread config, never on a command line)."""
    import sys

    from halo_harness.agent.cc_process import _repo_root_for_pythonpath
    env = dict(bridge.child_env())
    existing_pp = os.environ.get("PYTHONPATH", "")
    repo_root = _repo_root_for_pythonpath()
    env["PYTHONPATH"] = os.pathsep.join([repo_root, existing_pp]) if existing_pp else repo_root
    return {"command": sys.executable, "args": ["-m", "halo_harness.ccbridge"], "env": env,
            "startup_timeout_sec": 30, "tool_timeout_sec": _MCP_TOOL_TIMEOUT_S, "enabled": True}


def _thread_config(server: CodexAppServer, bridge: ToolBridgeServer, cwd) -> dict:
    """Per-thread config overrides: Halo's bridge as the only enabled MCP
    server (the user's own Codex MCP servers are switched off for this
    thread, the `--strict-mcp-config` counterpart) and no sandbox/approval
    boilerplate in the model's instructions (Halo's engine decides)."""
    servers = {BRIDGE_SERVER_NAME: _bridge_mcp_entry(bridge)}
    try:
        effective = (server.request("config/read", {"includeLayers": False, "cwd": str(cwd)}, timeout=15) or {})
        for name in ((effective.get("config") or {}).get("mcp_servers") or {}):
            if name != BRIDGE_SERVER_NAME:
                servers[name] = {"enabled": False}
    except CodexRpcError:
        pass
    return {"mcp_servers": servers, "include_apply_patch_tool": False, "include_permissions_instructions": False,
            "include_apps_instructions": False, "include_collaboration_mode_instructions": False}


def developer_instructions(session) -> str:
    from halo_harness.agent.cc_runtime import halo_context_parts
    parts = []
    if getattr(session, "agent_id", None):
        body = (getattr(session.session_context, "system_prompt", "") or "").strip()
        if body:
            parts.append(body[:_INSTRUCTIONS_CHARS // 2])
    parts.append(CX_DEVELOPER_INSTRUCTIONS)
    parts += halo_context_parts(session, name_desc_chars=200)
    text = "\n\n".join(p for p in parts if p)
    return text[:_INSTRUCTIONS_CHARS]


def _last_cx_thread_id(session_log) -> Optional[str]:
    for node in reversed(session_log.nodes()):
        if node.get("type") == "meta" and node.get("cx_thread_id"):
            return node["cx_thread_id"]
    return None


def ensure_cx_state(session) -> CxState:
    """This session's app-server + bridge + Codex thread, started lazily.
    A dead server (Esc escalation, crash) restarts here and resumes the
    same thread; `/fork` forks it; a thread Codex no longer has falls back
    to a fresh one primed with the conversation so far."""
    state: Optional[CxState] = getattr(session, "_cx_state", None)
    if state is not None and state.server.alive:
        return state
    if state is not None:
        _shutdown(state)
        session._cx_state = None

    err = preflight_cx()
    if err:
        raise CodexUnavailable(err)
    _maybe_warn_version_outside_tested_range()

    from halo_harness.agent.cc_runtime import bridge_list_tools
    bridge = ToolBridgeServer(session_id=str(_uuid_mod.uuid4()), list_tools_fn=lambda: bridge_list_tools(session),
                              call_tool_fn=lambda name, arguments: bridge_call_tool_cx(session, name, arguments))
    bridge.start()
    try:
        server = CodexAppServer.start(cwd=session.cwd, env=_child_env(session), extra_args=_server_args())
    except (CodexNotFoundError, OSError, CodexRpcError) as e:
        bridge.close()
        raise CodexUnavailable(f"cx: could not start `codex app-server`: {e}") from e

    model = session.model_ref.model
    params = {"model": model, "cwd": str(session.cwd), "approvalPolicy": "untrusted", "sandbox": "read-only",
              "config": _thread_config(server, bridge, session.cwd),
              "developerInstructions": developer_instructions(session)}
    fork = bool(getattr(session, "_cx_fork_session", False))
    session._cx_fork_session = False
    prior = _last_cx_thread_id(session.log)
    thread, primed = None, False
    try:
        if prior:
            try:
                method = "thread/fork" if fork else "thread/resume"
                thread = server.request(method, {**params, "threadId": prior, "excludeTurns": True},
                                        timeout=60)["thread"]
            except (CodexRpcError, KeyError, TypeError) as e:
                log.info("cx: could not resume thread %s (%s); starting a fresh one", prior, e)
        if thread is None:
            thread = server.request("thread/start", params, timeout=60)["thread"]
            primed = bool(prior)
    except (CodexRpcError, KeyError, TypeError) as e:
        server.close()
        bridge.close()
        raise CodexUnavailable(f"cx: Codex refused to start a conversation: {e}") from e

    state = CxState(server=server, bridge=bridge, thread_id=thread["id"], model=model)
    server.on_notification = lambda method, p: _on_notification(session, state, method, p)
    server.on_server_request = lambda rid, method, p: _on_server_request(session, state, rid, method, p)
    server.on_exit = lambda: _put(state, "EOF")
    if thread["id"] != prior:
        session.log.append_meta(cx_thread_id=thread["id"], cx_model=model)
    if primed or getattr(session, "_cx_pending_context", None):
        from halo_harness.agent.cc_runtime import _render_conversation_so_far
        text = getattr(session, "_cx_pending_context", None) or _render_conversation_so_far(session)
        session._cx_pending_context = None
        if text:
            state.pending_context.append(text)
    session._cx_state = state
    return state


def _put(state: CxState, item) -> None:
    q = state.active_queue
    if q is not None:
        q.put(item)


def _emit_for(state: CxState):
    return lambda ev: _put(state, ev)


def _shutdown(state: CxState) -> None:
    try:
        state.server.close()
    except Exception:
        pass
    try:
        state.bridge.close()
    except Exception:
        pass


def close_cx(session) -> None:
    """Stop this session's app-server and bridge (and any live `cx:`
    sub-agent children's); safe to call any number of times."""
    runtime = getattr(session, "agent_runtime", None)
    if runtime is not None:
        for child in list(getattr(runtime, "live_children", {}).values()):
            if child is not session:
                close_cx(child)
    state: Optional[CxState] = getattr(session, "_cx_state", None)
    if state is None:
        return
    session._cx_state = None
    _shutdown(state)


def prepare_conversation_so_far(session) -> None:
    """Switching INTO cx: mid-session: the prior transcript rides along
    with the first cx: turn (as an earlier input item of that same turn)."""
    from halo_harness.agent.cc_runtime import _render_conversation_so_far
    text = _render_conversation_so_far(session)
    if text:
        session._cx_pending_context = text


def notify_catalog_changed(session) -> None:
    state: Optional[CxState] = getattr(session, "_cx_state", None)
    if state is not None:
        state.bridge.notify_tools_changed()


def steer_cx(session, text: str) -> bool:
    """`turn/steer` into the running turn. False when no turn is running
    (the caller then starts a new turn with the text)."""
    state: Optional[CxState] = getattr(session, "_cx_state", None)
    if state is None or not session.busy or not state.turn_id or not state.server.alive:
        return False
    try:
        state.server.request("turn/steer", {"threadId": state.thread_id, "expectedTurnId": state.turn_id,
                                            "input": [{"type": "text", "text": text}]}, timeout=15)
    except CodexRpcError:
        return False
    session.log.append_user([{"type": "text", "text": text}], kind="steer")
    _put(state, events.notification("↳ sent to Codex"))
    return True


# ---- notifications (app-server reader thread) ------------------------------

def _usage_from_wire(u: dict) -> dict:
    return {"input_tokens": int(u.get("inputTokens") or 0), "output_tokens": int(u.get("outputTokens") or 0),
            "cache_read_input_tokens": int(u.get("cachedInputTokens") or 0),
            "reasoning_tokens": int(u.get("reasoningOutputTokens") or 0)}


def _on_notification(session, state: CxState, method: str, p: dict) -> None:
    if p.get("threadId") not in (None, state.thread_id):
        return
    turn_no = state.turn_no
    if method == "item/agentMessage/delta":
        state.delta_items.add(p.get("itemId"))
        _put(state, events.text_delta(p.get("delta") or "", turn=turn_no))
    elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
        _put(state, events.thinking_delta(p.get("delta") or "", turn=turn_no))
    elif method == "item/started":
        _on_item_started(state, p.get("item") or {})
    elif method == "item/completed":
        _on_item_completed(session, state, p.get("item") or {})
    elif method == "thread/tokenUsage/updated":
        # Summed per response (`last`), never `total`: the thread total
        # also counts turns from before a restart or resume.
        usage = p.get("tokenUsage") or {}
        for k, v in _usage_from_wire(usage.get("last") or {}).items():
            state.turn_usage[k] = state.turn_usage.get(k, 0) + v
        window = usage.get("modelContextWindow")
        if isinstance(window, int) and window > 0:
            from halo_harness.providers.cx_models import record_context_window
            record_context_window(state.model, window)
    elif method == "account/rateLimits/updated":
        from halo_harness.providers.cx_models import record_rate_limits
        rl = p.get("rateLimits") or {}
        record_rate_limits(rl)
        if rl.get("rateLimitReachedType"):
            _put(state, events.notification(f"Codex usage limit reached ({rl['rateLimitReachedType']}) -- "
                                            f"see /providers for when it resets", level="error"))
    elif method == "error":
        err = p.get("error") or {}
        msg = err.get("message") or "Codex reported an error"
        if p.get("willRetry"):
            _put(state, events.notification(f"Codex: {msg} (retrying)"))
        else:
            state.turn_error = msg
    elif method == "thread/compacted":
        _put(state, events.notification("Codex compacted this conversation's context"))
    elif method == "mcpServer/startupStatus/updated":
        if p.get("name") == BRIDGE_SERVER_NAME and p.get("status") == "failed":
            _put(state, events.notification(f"cx: the tool bridge did not start ({p.get('error') or 'failed'}) -- "
                                            f"Halo's tools are unavailable this turn.", level="error"))
    elif method == "turn/completed":
        # Matched against the turn id in `_drain_turn`, never here: a quick
        # turn can complete before the turn/start response (and so the id)
        # has been read on the worker thread.
        _put(state, ("TURN_DONE", p.get("turn") or {}))


def _on_item_started(state: CxState, item: dict) -> None:
    typ = item.get("type")
    if typ == "mcpToolCall" and item.get("server") == BRIDGE_SERVER_NAME:
        with state.cond:
            state.pending_tool_uses.append({"id": item.get("id"), "name": item.get("tool"),
                                            "input": item.get("arguments") or {}})
            state.cond.notify_all()
    elif typ == "fileChange":
        state.file_changes[item.get("id")] = item.get("changes") or []


def _on_item_completed(session, state: CxState, item: dict) -> None:
    typ = item.get("type")
    turn_no = state.turn_no
    if typ == "agentMessage":
        text = item.get("text") or ""
        if text:
            session.log.append_assistant(content=[{"type": "text", "text": text}])
            if item.get("id") not in state.delta_items:
                _put(state, events.text_delta(text, turn=turn_no))
            elif item.get("phase") == "commentary":
                _put(state, events.text_delta("\n\n", turn=turn_no))
    elif typ in ("fileChange", "commandExecution"):
        native = state.native_items.pop(item.get("id"), None)
        if native is None:
            return
        status = item.get("status") or "completed"
        ok = status == "completed"
        out = (item.get("aggregatedOutput") or "") if typ == "commandExecution" else ""
        text = out or ("Applied by Codex." if ok else f"Not applied ({status}).")
        session.log.append_tool_result(tool_use_id=native["tool_use_id"], content=text, is_error=not ok,
                                       tool=native["name"])
        _put(state, events.Event("tool_result", {"id": native["tool_use_id"], "ok": ok, "summary": text[:200]},
                                 turn=turn_no))
    elif typ == "mcpToolCall" and item.get("server") != BRIDGE_SERVER_NAME:
        _put(state, events.notification(f"Codex called {item.get('server')}.{item.get('tool')} directly"))


# ---- server requests: approvals, elicitations, user input -------------------

def _native_decision(session, state: CxState, item_id: str, name: str, tool_input: dict) -> bool:
    """Runs one native Codex action through Halo's engine as `name`
    (Write/Edit/Bash) and logs it like any tool call. True = allowed."""
    from halo_harness.agent.cc_runtime import resolve_bridged_call
    tool_use_id = f"cx_{item_id}_{_uuid_mod.uuid4().hex[:6]}"
    turn_no, emit = state.turn_no, _emit_for(state)
    session.log.append_assistant(content=[{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}])
    emit(events.Event("tool_use_ready", {"id": tool_use_id, "name": name, "input": tool_input, "repaired": False},
                      turn=turn_no))
    item = resolve_bridged_call(session, turn_no, tool_use_id, name, tool_input, emit=emit)
    if item.get("ready"):
        state.native_items[item_id] = {"tool_use_id": tool_use_id, "name": name}
        return True
    text = item.get("text") or "Permission denied."
    session.log.append_tool_result(tool_use_id=tool_use_id, content=text, is_error=True, tool=name,
                                   error_class="permission_denied")
    emit(events.Event("tool_result", {"id": tool_use_id, "ok": False, "summary": text[:200]}, turn=turn_no))
    return False


def _file_change_inputs(changes: list) -> list:
    out = []
    for ch in changes or []:
        kind = ((ch.get("kind") or {}).get("type") if isinstance(ch.get("kind"), dict) else ch.get("kind")) or "update"
        path, diff = ch.get("path") or "", ch.get("diff") or ""
        if kind == "add":
            out.append(("Write", {"file_path": path, "content": diff}))
        else:
            out.append(("Edit", {"file_path": path, "diff": diff, "change": kind}))
    return out


def _on_server_request(session, state: CxState, rid, method: str, p: dict) -> None:
    server = state.server
    if method == "mcpServer/elicitation/request":
        meta = p.get("_meta") or {}
        # Halo's own engine decides every bridged call inside the bridge.
        ok = p.get("serverName") == BRIDGE_SERVER_NAME and meta.get("codex_approval_kind") == "mcp_tool_call"
        server.respond(rid, {"action": "accept", "content": {}} if ok else {"action": "decline"})
    elif method == "item/fileChange/requestApproval":
        item_id = p.get("itemId") or ""
        allowed = True
        for name, tool_input in _file_change_inputs(state.file_changes.get(item_id) or []) or [("Edit", {})]:
            allowed = _native_decision(session, state, f"{item_id}", name, tool_input) and allowed
            if not allowed:
                break
        server.respond(rid, {"decision": "accept" if allowed else "decline"})
    elif method == "item/commandExecution/requestApproval":
        command = p.get("command") or ""
        allowed = _native_decision(session, state, p.get("itemId") or "", "Bash", {"command": command})
        server.respond(rid, {"decision": "accept" if allowed else "decline"})
    elif method == "item/tool/requestUserInput":
        server.respond(rid, {"answers": _ask_user(session, state, p)})
    elif method in ("execCommandApproval", "applyPatchApproval"):
        server.respond(rid, {"decision": "denied"})
    else:
        server.respond_error(rid, f"halo does not handle {method}", code=-32601)


def _ask_user(session, state: CxState, p: dict) -> dict:
    """Codex's request_user_input, answered through Halo's question dock."""
    from halo_harness.agent.cc_runtime import resolve_bridged_call
    questions = [q for q in (p.get("questions") or []) if isinstance(q, dict)]
    if not questions:
        return {}
    ask = {"questions": [{"question": q.get("question") or "", "header": (q.get("header") or "Codex")[:12],
                          "multiSelect": False,
                          "options": [{"label": o.get("label") or "", "description": o.get("description") or ""}
                                      for o in (q.get("options") or [])]} for q in questions]}
    tool_use_id = f"cx_ask_{_uuid_mod.uuid4().hex[:8]}"
    item = resolve_bridged_call(session, state.turn_no, tool_use_id, "AskUserQuestion", ask, emit=_emit_for(state))
    result = item.get("result")
    answer = getattr(result, "content", None) if result is not None else None
    text = answer if isinstance(answer, str) else (item.get("text") or "")
    return {q.get("id"): {"answers": [text]} for q in questions if q.get("id")}


# ---- bridged tool calls (bridge connection thread) --------------------------

def _take_tool_use_id(state: CxState, name: str, tool_input: dict) -> str:
    """Codex announces each MCP call (`item/started`) just before calling
    the bridge: match on (name, arguments) first, on name alone once the
    wait runs out, and mint an id as the last resort."""
    deadline = time.monotonic() + _TOOL_USE_ID_WAIT_S
    with state.cond:
        while True:
            for i, pending in enumerate(state.pending_tool_uses):
                if pending.get("name") == name and pending.get("input") == (tool_input or {}):
                    del state.pending_tool_uses[i]
                    return pending.get("id") or f"cx_{_uuid_mod.uuid4().hex[:12]}"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                for i, pending in enumerate(state.pending_tool_uses):
                    if pending.get("name") == name:
                        del state.pending_tool_uses[i]
                        return pending.get("id") or f"cx_{_uuid_mod.uuid4().hex[:12]}"
                return f"cx_{_uuid_mod.uuid4().hex[:12]}"
            state.cond.wait(timeout=min(remaining, 0.25))


def bridge_call_tool_cx(session, name: str, arguments: dict) -> dict:
    from halo_harness.agent.cc_runtime import _resolve_and_dispatch_bridged_call, _wire_result
    state: Optional[CxState] = getattr(session, "_cx_state", None)
    if state is None:
        return _wire_result("cx: no active Codex session", True)
    tool_input = arguments or {}
    tool_use_id = _take_tool_use_id(state, name, tool_input)
    turn_no, emit = state.turn_no, _emit_for(state)
    session.log.append_assistant(content=[{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}])
    emit(events.Event("tool_use_ready", {"id": tool_use_id, "name": name, "input": tool_input, "repaired": False},
                      turn=turn_no))
    with state.lock:
        state.in_flight.add(tool_use_id)
    try:
        return _resolve_and_dispatch_bridged_call(session, turn_no, tool_use_id, name, tool_input, emit=emit)
    except Exception as e:
        log.exception("bridge_call_tool_cx: dispatch failed for %s", name)
        text = f"cx bridge: internal dispatch error: {type(e).__name__}: {e}"
        try:
            session.log.append_tool_result(tool_use_id=tool_use_id, content=text, is_error=True, tool=name,
                                           error_class="other")
        except Exception:
            pass
        emit(events.Event("tool_result", {"id": tool_use_id, "ok": False, "summary": text[:200]}, turn=turn_no))
        return _wire_result(text, True)
    finally:
        with state.lock:
            state.in_flight.discard(tool_use_id)


# ---- turn execution (the Session's own worker thread) -----------------------

def _image_input(block: dict) -> Optional[dict]:
    source = block.get("source") or {}
    if block.get("type") == "image" and source.get("type") == "base64" and source.get("data"):
        return {"type": "image", "url": f"data:{source.get('media_type') or 'image/png'};base64,{source['data']}"}
    return None


def _turn_effort(session, model_id: str) -> Optional[str]:
    """Halo's effort, clamped to what this model accepts (from model/list)."""
    effort = getattr(session, "effort", None)
    if not effort:
        return None
    from halo_harness.providers.cx_models import profile_fields_for_cx_model
    allowed = list(profile_fields_for_cx_model(model_id).get("efforts") or ())
    if not allowed or effort in allowed:
        return effort
    from halo_harness.providers.profiles import EFFORT_LEVELS
    order = list(EFFORT_LEVELS) + ["ultra"]
    rank = order.index(effort) if effort in order else 2
    below = [e for e in allowed if e in order and order.index(e) <= rank]
    return below[-1] if below else allowed[0]


def _start_turn(session, state: CxState, inputs: list) -> str:
    # Reasoning summaries stream as Halo's visible thinking text.
    params = {"threadId": state.thread_id, "input": inputs, "summary": "auto"}
    effort = _turn_effort(session, state.model)
    if effort:
        params["effort"] = effort
    res = state.server.request("turn/start", params, timeout=60)
    return (res.get("turn") or {}).get("id")


def _log_turn_usage(session, state: CxState, turn_no: int, turn: dict) -> "events.Event":
    usage = dict(state.turn_usage)
    state.turn_usage = {}
    session.cost_meter.add_usage("cx", dict(usage))
    status = turn.get("status") or "completed"
    session.log.append_usage(usage, cost_usd=None, model=session.model_ref.raw, route="cx", provider="cx",
                             finish_reason=status, latency_ms=turn.get("durationMs"),
                             status="ok" if status == "completed" else "error", estimate=False)
    return events.message_end(turn=turn_no, stop_reason=status, usage=usage, cost_usd=None)


def turn_body_cx(session, turn_no: int, text: str, *, images: Optional[list] = None,
                 hook_context: Optional[str] = None):
    """The cx: equivalent of `Session._turn_body`; always ends with
    `turn_done`. Job/agent notices, hook context and a carried-over
    conversation ride as earlier input items of the same Codex turn."""
    try:
        state = ensure_cx_state(session)
    except CodexUnavailable as e:
        yield events.error(str(e), turn=turn_no, err_type="cx_unavailable")
        yield events.turn_done(turn=turn_no, reason="error")
        return

    q: "queue.Queue" = queue.Queue()
    state.active_queue, state.turn_no, state.turn_error = q, turn_no, None
    state.delta_items.clear()
    reason = "end_turn"
    try:
        inputs = [{"type": "text", "text": t} for t in state.pending_context if t]
        state.pending_context.clear()
        for ev in list(session._apply_pending_job_notices(turn_no)) + list(session._apply_pending_agent_notices(turn_no)):
            if ev.kind == "user_message" and ev.data.get("text"):
                inputs.append({"type": "text", "text": ev.data["text"]})
            yield ev
        if hook_context:
            inputs.append({"type": "text", "text": hook_context})
        inputs.append({"type": "text", "text": text})
        inputs += [img for img in (_image_input(b) for b in (images or []) if isinstance(b, dict)) if img]
        yield events.message_start(turn=turn_no, model=session.model_ref.raw)

        while True:
            state.turn_usage = {}  # before turn/start: a quick turn can report usage before it returns
            try:
                state.turn_id = _start_turn(session, state, inputs)
            except CodexRpcError as e:
                yield events.error(f"Codex did not start this turn: {e}", turn=turn_no, err_type="cx_turn_start")
                reason = "error"
                break
            reason, turn = yield from _drain_turn(session, state, q, turn_no)
            if turn is not None:
                yield _log_turn_usage(session, state, turn_no, turn)
            if reason != "end_turn" or session.hook_runner is None or not session.hook_runner.has_hooks("Stop"):
                break
            from halo_harness.agent.cc_runtime import _last_assistant_text
            outcome = session._run_hook_stop("Stop", last_assistant_message=_last_assistant_text(session.log),
                                             prompt_id=f"turn_{turn_no}", abort=session.abort)
            for msg in outcome.system_messages:
                yield events.notification(msg)
            if not outcome.blocked:
                break
            continuation = outcome.block_reason or "Please continue."
            session.log.append_user([{"type": "text", "text": continuation}], kind="continuation")
            yield events.user_message(continuation, turn=turn_no)
            inputs = [{"type": "text", "text": continuation}]
    finally:
        state.turn_id = None
        state.active_queue = None
        from halo_harness.agent.invariants import synthesize_missing_results
        synthesize_missing_results(session.log, reason={
            "interrupted": "Tool call interrupted by user",
            "error": "Tool call never completed (Codex ended or errored)"}.get(reason, "Tool call never received a result"))
    yield session.status_event(phase="idle", turn=turn_no)
    yield events.turn_done(turn=turn_no, reason=reason)


def _drain_turn(session, state: CxState, q: "queue.Queue", turn_no: int):
    """Yields this turn's events until `turn/completed`; returns
    `(reason, turn)`. Esc sends `turn/interrupt`; if Codex does not wind
    the turn down within the grace period the app-server is stopped (the
    next turn restarts it and resumes the same thread)."""
    interrupt_deadline = None
    while True:
        try:
            item = q.get(timeout=0.05)
        except queue.Empty:
            if session.abort.is_set() and interrupt_deadline is None:
                interrupt_deadline = time.monotonic() + _INTERRUPT_GRACE_S
                try:
                    state.server.request("turn/interrupt", {"threadId": state.thread_id, "turnId": state.turn_id},
                                         timeout=_INTERRUPT_GRACE_S)
                except CodexRpcError:
                    pass
            if interrupt_deadline is not None and time.monotonic() > interrupt_deadline:
                close_cx(session)
                return "interrupted", None
            continue
        if isinstance(item, events.Event):
            yield item
            continue
        if item == "EOF":
            if session.abort.is_set():
                return "interrupted", None
            tail = state.server.stderr_tail().strip()
            msg = "the codex app-server ended unexpectedly" + (f" -- {tail.splitlines()[-1][:300]}" if tail else "")
            yield events.error(msg, turn=turn_no, err_type="cx_eof")
            return "error", None
        if isinstance(item, tuple) and item[0] == "TURN_DONE":
            turn = item[1]
            if turn.get("id") and state.turn_id and turn.get("id") != state.turn_id:
                continue  # a previous turn's late completion
            status = turn.get("status")
            if status == "interrupted" or session.abort.is_set():
                return "interrupted", turn
            if status == "failed" or state.turn_error:
                err = (turn.get("error") or {}).get("message") or state.turn_error or "Codex reported an error"
                yield events.error(str(err), turn=turn_no, err_type="cx_turn_failed")
                return "error", turn
            return "end_turn", turn


def one_shot_cx_call(model: str, system_text: str, user_text: str, *, timeout_s: float = 60.0) -> str:
    """A stateless call for titles, prompt hooks and /improve drafts: an
    ephemeral thread on a short-lived app-server, no tools, no MCP."""
    try:
        server = CodexAppServer.start(cwd=os.path.expanduser("~"), env=_child_env(None), extra_args=_server_args())
    except (CodexNotFoundError, OSError, CodexRpcError) as e:
        raise RuntimeError(f"cx: small-model call unavailable: {e}") from e
    done, texts = threading.Event(), []

    def on_note(method: str, p: dict) -> None:
        item = p.get("item") or {}
        if method == "item/completed" and item.get("type") == "agentMessage" and item.get("text"):
            texts.append(item["text"])
        elif method == "turn/completed":
            done.set()

    server.on_notification = on_note
    server.on_server_request = lambda rid, method, p: server.respond_error(rid, "no tools in this call")
    server.on_exit = done.set
    try:
        thread = server.request("thread/start", {"model": model, "ephemeral": True, "approvalPolicy": "never",
                                                 "sandbox": "read-only", "developerInstructions": system_text,
                                                 "config": {"include_apply_patch_tool": False}},
                                timeout=timeout_s)["thread"]
        server.request("turn/start", {"threadId": thread["id"], "effort": "low",
                                      "input": [{"type": "text", "text": user_text}]}, timeout=timeout_s)
        if not done.wait(timeout_s):
            raise RuntimeError(f"cx: small-model call timed out after {timeout_s}s")
    except (CodexRpcError, KeyError, TypeError) as e:
        raise RuntimeError(f"cx: small-model call failed: {e}") from e
    finally:
        server.close()
    return texts[-1] if texts else ""

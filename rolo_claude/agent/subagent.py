"""rolo_claude.agent.subagent -- the Agent/Task tool's real implementation
(H6 scope B): `AgentRuntime` (what a session's Agent-tool calls need beyond
a bare ToolContext), spawning a child `agent.loop.Session` per call with a
fresh log under `<parent_session_dir>/subagents/agent-<id>.jsonl` (+ a
`.meta.json` beside it), depth-1 + <=4-concurrent enforcement, background
completion delivered as a user-role notice on the PARENT's next turn (dsh:
"background jobs ... report completion as a user-role notice in the next
step"), resumable `task_id` (`<task_result task_id="...">` wrapping, OpenCode
adopt item), and SubagentStart/SubagentStop hook firing (tagged with the
child's own `agent_id`/`agent_type` via its own HookRunner).

`agent/loop.py`'s `_dispatch_tools` calls `run_agent_call` DIRECTLY (a
dedicated code path, like AskUserQuestion/ExitPlanMode -- never through the
generic `tool_registry.dispatch()`) so it can run several calls
CONCURRENTLY (a real `ThreadPoolExecutor`, up to `MAX_CONCURRENT_AGENTS`)
and still forward every child event, tagged, into the parent's own event
stream once the batch completes. `tools/agent.py`'s `AgentTool.run()` calls
the SAME function as a correctness fallback for any caller that dispatches
it generically instead (a unit test, or a future caller) -- it still does
the real work, just without the intermediate child events being visible.

H5c finding 8: a FOREGROUND child session's own permission "ask" is now
surfaced LIVE (an agent-tagged, answerable `permission_request` card),
never auto-denied, whenever the PARENT is interactive -- `child.
_subagent_live_asks` (set below, deliberately never `child.interactive`
itself, which also gates AskUserQuestion/ExitPlanMode -- out of this
finding's scope and would otherwise hang a child forever on those, since
only `_permission_waiters` is shared, not `_question_waiters`/
`_plan_waiters`) and a shared `_permission_waiters` dict (same pattern as
the shared `abort` Event) are what make this possible: the child's own
`_resolve_tool_call` parks its waiter in the SAME dict the PARENT's
`resolve_permission` (the UI's `Controller.answer_permission`) already
reads from, so the existing card-answer plumbing needs no changes at all.
This REQUIRES `_run_child_to_completion` to stream every child event live
(the `on_event` callback below) rather than buffer the whole turn into a
list first -- a child blocked waiting for its own permission answer would
never finish "buffering" in the first place, so its `permission_request`
would never reach anything. A BACKGROUND child (`run_in_background`/
`spec.background`) is deliberately EXCLUDED (`_subagent_live_asks` stays
False for it): its events are never forwarded to any live stream at all
(see `_bg_run`'s own `del child_events` below), so a live card for it
would have nowhere to be shown and would just hang the background thread
-- it keeps the old immediate-denial fallback instead. Print mode (`-p`,
`parent.interactive` False) also keeps the old fallback for every child,
foreground or not: deny + `permission_denials`, exactly like Claude
Code's own "print mode -> deny" rule for every session, not just `-p`
itself.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rolo_claude import events
from rolo_claude.config.agents_md import AgentSpec, resolve_agent_model

MAX_CONCURRENT_AGENTS = 4
MAX_DEPTH = 1
RESULT_CAP = 30_000


@dataclass
class AgentRuntime:
    """Held as `Session.agent_runtime` and threaded through every
    `ToolContext` as `agent_runtime=` (tools/base.py) -- untyped there to
    avoid a hard import-order requirement (this module imports
    `agent.loop.Session`, so `tools/base.py` cannot import this module)."""
    parent: object                                  # agent.loop.Session
    agents: "dict[str, AgentSpec]" = field(default_factory=dict)
    routes: dict = field(default_factory=dict)
    depth: int = 0
    tasks: dict = field(default_factory=dict)        # task_id -> {"child_session_id", "spec_name", "cwd"}
    lock: "threading.Lock" = field(default_factory=threading.Lock)


def _new_agent_id() -> str:
    return uuid.uuid4().hex[:8]


def _child_log_paths(parent, agent_id: str):
    """`<parent_session_dir>/subagents/agent-<id>.jsonl` (brief B) -- built
    by constructing an ordinary `SessionLog` and then redirecting its
    `dir`/`path` (SAFE: done immediately after construction, before
    `Session.__init__` ever calls `.nodes()`/appends anything). Existing
    content is loaded up front so a task_id RESUME naturally lands in
    `Session.__init__`'s own "resume an existing log" branch with zero
    extra plumbing -- a fresh spawn's file doesn't exist yet, so `_nodes`
    stays `[]` and the "brand new session" branch runs instead."""
    from rolo_claude.agent.log import SessionLog

    parent_dir = parent.log.dir / parent.log.session_id
    subdir = parent_dir / "subagents"
    subdir.mkdir(parents=True, exist_ok=True)
    session_id = f"agent-{agent_id}"
    log = SessionLog(parent.cwd, session_id=session_id)
    log.dir = subdir
    log.path = subdir / f"{session_id}.jsonl"
    if log.path.exists():
        log._nodes = log.read_all()
    return log, subdir / f"{session_id}.meta.json"


def _write_meta(meta_path: Path, data: dict) -> None:
    try:
        existing = {}
        if meta_path.exists():
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    existing.update(data)
    try:
        meta_path.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass


def _build_child_session(*, runtime: AgentRuntime, spec: AgentSpec, agent_id: str,
                          model_override: Optional[str], parent_tool_use_id: str, background: bool = False):
    """A real `agent.loop.Session` for `spec`: its OWN fresh (or resumed)
    log, a tool subset frozen from the PARENT's own catalog (never grows
    it -- brief B), its `body` as the full system prompt (CLAUDE.md/memory
    per `AgentSpec.skips_claude_md`/`includes_memory`), a resolved model,
    and a permission mode/engine of its own (sharing the parent's rules,
    scoped by `spec.permission_mode` when set)."""
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.hooks import HookRunner
    from rolo_claude.permissions import PermissionEngine

    parent = runtime.parent
    child_log, meta_path = _child_log_paths(parent, agent_id)

    catalog_names = (parent.session_catalog.names if parent.session_catalog is not None
                      else parent.tool_registry.names())
    tool_names = spec.resolved_tools(catalog_names)
    child_registry = parent.tool_registry.filtered(tool_names)

    ctx = SessionContext(
        cwd=parent.cwd, model_label=parent.model_label, model_family="generic",
        bare=spec.skips_claude_md(), tool_registry=child_registry, mcp_servers=None,
    )
    ctx.system_prompt = spec.body or spec.description
    if not spec.includes_memory():
        ctx.memory_snapshot_text = lambda: ""  # D-CFG: "no memory index unless general-purpose"

    model_ref, model_profile = resolve_agent_model(
        invocation_model=model_override, frontmatter_model=spec.model,
        env=parent.tool_env, settings=getattr(parent.session_context, "settings", None),
        parent_ref=parent.model_ref, parent_profile=parent.model_profile,
        parent_small_ref=parent.small_model_ref, state_dir=parent.state_dir, routes=runtime.routes,
    )

    permission_mode = spec.permission_mode or parent.permission_engine.mode
    child_engine = PermissionEngine(
        deny_rules=parent.permission_engine.deny_rules, ask_rules=parent.permission_engine.ask_rules,
        allow_rules=list(parent.permission_engine.allow_rules), mode=permission_mode,
        cwd=parent.cwd, extra_dirs=parent.permission_engine.extra_dirs,
        print_mode=parent.permission_engine.print_mode,
    )

    hook_runner = None
    if parent.hook_runner is not None:
        hook_runner = HookRunner(
            parent.hook_runner.hooks_by_event, cwd=parent.cwd, session_id=child_log.session_id,
            transcript_path=str(child_log.path), effective_env=parent.hook_runner.effective_env,
            permission_mode=permission_mode, effort=(spec.effort or parent.effort),
            mcp_manager=parent.mcp_manager, prompt_caller=parent.hook_runner.prompt_caller,
            enabled=parent.hook_runner.enabled, agent_id=agent_id, agent_type=spec.name,
        )

    child = Session(
        cwd=parent.cwd, model_ref=model_ref, model_profile=model_profile, creds=parent.creds,
        state_dir=parent.state_dir, model_label=model_ref.raw, session_context=ctx,
        small_model_ref=parent.small_model_ref, session_log=child_log,
        max_turns=(spec.max_turns or parent.max_turns), effort=(spec.effort or parent.effort),
        permission_engine=child_engine, session_catalog=None, mcp_manager=parent.mcp_manager,
        hook_runner=hook_runner,
        # bug fix: without these, a child ignores whatever custom routing
        # the PARENT was actually given (a mock upstream in tests; a
        # self-hosted proxy via BRIDGE_OPENROUTER_BASE_URL in real use) and
        # falls back to `creds.base_url`/no extra headers, which usually
        # happens to match but is not guaranteed to (`providers/stream.py`:
        # "base_url = req.openrouter_base_url or req.creds.base_url").
        openrouter_base_url=parent.openrouter_base_url, extra_headers=dict(parent.extra_headers),
        # H6 scope B must-do ("sub-agents reuse steer/abort/... plumbing"):
        # share the PARENT's own abort Event rather than letting Session's
        # default (a fresh, private Event) leave the child deaf to it --
        # `_run_child_to_completion` below is a bare drain loop with no
        # abort check of its own, so without this an Esc/kill during a
        # running sub-agent had NO effect at all until the child finished
        # organically; sharing the object means every existing
        # `self.abort.is_set()` check already threaded through the
        # child's own `_step`/retry waits/streaming now reacts to the
        # SAME signal, with no new logic needed on either side.
        abort=parent.abort,
    )
    if hook_runner is not None:
        hook_runner.prompt_caller = child._call_model_for_hook
    child.interactive = False  # deliberately NEVER True -- see module docstring (AskUserQuestion/ExitPlanMode stay out of scope)
    # H5c finding 8: a live, answerable permission card, only for a
    # FOREGROUND child of an interactive parent -- see module docstring.
    child._subagent_live_asks = bool(parent.interactive) and not background
    child._permission_waiters = parent._permission_waiters
    child.agent_runtime = AgentRuntime(parent=child, agents=runtime.agents, routes=runtime.routes,
                                        depth=runtime.depth + 1, tasks=runtime.tasks, lock=runtime.lock)
    _write_meta(meta_path, {
        "agent_id": agent_id, "type": spec.name, "description": spec.description,
        "model": model_ref.raw, "parent_tool_use_id": parent_tool_use_id,
        "started": time.time(), "status": "running", "session_id": child_log.session_id,
    })
    return child, meta_path


def _fire_subagent_hook(child, event_name: str) -> None:
    if child.hook_runner is None:
        return
    try:
        if event_name == "SubagentStop":
            child.hook_runner.run_stop("SubagentStop")
        else:
            child.hook_runner.run(event_name, child.hook_runner.payload(event_name))
    except Exception:
        pass


def _tag(ev, *, agent_id: str, parent_tool_use_id: str):
    ev.agent_id = agent_id
    if isinstance(ev.data, dict):
        ev.data = {**ev.data, "parent_tool_use_id": parent_tool_use_id}
    return ev


def _run_child_to_completion(child, prompt_text: str, *, agent_id: str, parent_tool_use_id: str,
                              on_event=None) -> list:
    """H5c finding 8: `on_event`, when given, is called SYNCHRONOUSLY on
    THIS thread with each tagged event the INSTANT it is produced -- never
    buffered -- so a live caller (loop.py's `_run_agent_batch_live`) can
    forward it (most importantly a `permission_request`, right before the
    child's OWN thread then blocks waiting for that exact card's answer)
    to a live stream while the child is still mid-turn. The full list is
    still returned too (unchanged contract: `tools/agent.py`'s direct-call
    fallback, and a background child's own parity/debuggability copy,
    still just want the whole thing at the end, no live channel needed)."""
    out = []
    for ev in child.turn(prompt_text):
        tagged = _tag(ev, agent_id=agent_id, parent_tool_use_id=parent_tool_use_id)
        out.append(tagged)
        if on_event is not None:
            on_event(tagged)
    return out


def _final_text_from_log(child) -> str:
    for node in reversed(child.log.nodes()):
        if node.get("type") == "assistant":
            parts = [b.get("text", "") for b in (node.get("content") or [])
                     if isinstance(b, dict) and b.get("type") == "text"]
            text = "".join(parts).strip()
            if text:
                return text
    return "(the sub-agent finished without producing a final text answer)"


def _wrap_task_result(text: str, task_id: str) -> str:
    """H6 scope F (OpenCode adopt item): resumable sub-agent tasks -- a
    later `Agent(task_id=..., prompt=...)` resumes the SAME child session
    with its full prior context (via `Session.__init__`'s own resume path,
    reached because `_child_log_paths` preloads the existing log)."""
    return f'<task_result task_id="{task_id}">\n{text}\n</task_result>'


def run_agent_call(*, runtime: AgentRuntime, tool_id: str, tool_input: dict, tool_name: str,
                    on_event=None) -> "tuple[list, object]":
    """`(events, ToolResult)` for ONE Agent/Task tool_use. Never raises --
    every failure mode becomes an `is_error` ToolResult so a sub-agent's
    own bug can never crash the parent's turn. `on_event` (H5c finding 8):
    forwarded to the FOREGROUND child's own `_run_child_to_completion` so
    its events (most importantly `permission_request`) reach a live caller
    the instant they happen -- never used for a background child (see
    `_bg_run`, whose events are not forwarded to any live stream)."""
    from rolo_claude.tools.base import ToolResult
    from rolo_claude.tools.truncate import spill_and_truncate

    if runtime.depth >= MAX_DEPTH:
        return [], ToolResult(
            "Sub-agents cannot spawn further sub-agents (depth limit reached) -- finish this task "
            "yourself instead of delegating further.", is_error=True,
        )

    tool_input = tool_input if isinstance(tool_input, dict) else {}
    parent = runtime.parent
    task_id = tool_input.get("task_id")
    if task_id:
        return _resume_task(runtime, task_id, tool_input, tool_id, on_event=on_event)

    subagent_type = tool_input.get("subagent_type") or "general-purpose"
    spec = runtime.agents.get(subagent_type)
    if spec is None:
        known = ", ".join(sorted(runtime.agents)) or "(none discovered)"
        return [], ToolResult(f"Unknown subagent_type {subagent_type!r}. Known types: {known}", is_error=True)
    restriction = getattr(parent, "agent_type_restriction", None)
    if restriction is not None and subagent_type not in restriction:
        return [], ToolResult(f"This session may not spawn subagent_type {subagent_type!r}.", is_error=True)

    description = tool_input.get("description") or spec.description[:60]
    prompt = tool_input.get("prompt") or spec.initial_prompt or description
    background = bool(tool_input.get("run_in_background")) or spec.background
    model_override = tool_input.get("model")

    agent_id = _new_agent_id()
    child, meta_path = _build_child_session(
        runtime=runtime, spec=spec, agent_id=agent_id, model_override=model_override, parent_tool_use_id=tool_id,
        background=background,
    )
    new_task_id = uuid.uuid4().hex[:12]
    with runtime.lock:
        runtime.tasks[new_task_id] = {"child_session_id": child.log.session_id, "spec_name": spec.name,
                                        "cwd": str(parent.cwd)}

    start_ev = events.Event("subagent_start", {"agent_id": agent_id, "name": spec.name, "description": description,
                                                 "parent_tool_use_id": tool_id, "task_id": new_task_id})
    start_ev.agent_id = agent_id
    _fire_subagent_hook(child, "SubagentStart")
    if on_event is not None:
        # H5c finding 8: emitted LIVE too (not just in the returned list),
        # so a streaming caller's UI shows "sub-agent started" before the
        # child's own first event, not only once the whole call returns.
        on_event(start_ev)

    if background:
        _write_meta(meta_path, {"status": "background"})

        def _bg_run():
            child_events = _run_child_to_completion(child, prompt, agent_id=agent_id, parent_tool_use_id=tool_id)
            text = _final_text_from_log(child)
            _fire_subagent_hook(child, "SubagentStop")
            _write_meta(meta_path, {"status": "completed"})
            # H6/D10 (see the foreground path's own comment): merge even
            # for a background sub-agent, under the same lock its own
            # pending-notice append already uses.
            with parent._agent_notices_lock:
                parent.permission_denials.extend(child.permission_denials)
            notice = (f"[Background sub-agent '{spec.name}' finished (task_id={new_task_id})]\n"
                      f"{_wrap_task_result(text, new_task_id)}")
            with parent._agent_notices_lock:
                parent._pending_agent_notices.append(notice)
            del child_events  # collected for parity/debuggability only; not forwarded (parent turn has ended)

        threading.Thread(target=_bg_run, daemon=True, name=f"subagent-{agent_id}").start()
        result = ToolResult(
            f"Sub-agent '{spec.name}' started in the background (task_id={new_task_id}). Its result "
            f"will be reported to you as a notice once it finishes -- continue with other work."
        )
        return [start_ev], result

    child_events = _run_child_to_completion(child, prompt, agent_id=agent_id, parent_tool_use_id=tool_id,
                                             on_event=on_event)
    text = _final_text_from_log(child)
    _fire_subagent_hook(child, "SubagentStop")
    _write_meta(meta_path, {"status": "completed"})
    # H6 known v1 gap (D10) / B must-do: a non-interactive child's own
    # "ask" denials (agent/loop.py's `_resolve_tool_call`) land in the
    # CHILD's own `permission_denials` list, which nothing outside this
    # function would otherwise ever read -- merged into the PARENT's here
    # so print mode's top-level result (headless.py reads `session.
    # permission_denials`, the TOP session only) actually surfaces a
    # sub-agent's denied tool calls instead of silently losing them.
    parent.permission_denials.extend(child.permission_denials)
    end_ev = events.Event("subagent_end", {"agent_id": agent_id, "name": spec.name, "parent_tool_use_id": tool_id,
                                             "task_id": new_task_id})
    end_ev.agent_id = agent_id
    if on_event is not None:
        on_event(end_ev)  # H5c finding 8: live too, same reasoning as start_ev above

    wrapped = _wrap_task_result(text, new_task_id)
    capped = spill_and_truncate(wrapped, cap=RESULT_CAP, session_dir=parent.log.dir / parent.log.session_id,
                                 tool_use_id=tool_id)
    return [start_ev, *child_events, end_ev], ToolResult(capped)


def _resume_task(runtime: AgentRuntime, task_id: str, tool_input: dict, tool_id: str, *, on_event=None):
    from rolo_claude.tools.base import ToolResult
    from rolo_claude.tools.truncate import spill_and_truncate

    parent = runtime.parent
    record = runtime.tasks.get(task_id)
    if record is None:
        return [], ToolResult(f"Unknown task_id {task_id!r} -- it may belong to a different session.", is_error=True)
    spec = runtime.agents.get(record["spec_name"])
    if spec is None:
        return [], ToolResult(f"The agent type for task_id {task_id!r} is no longer available.", is_error=True)

    agent_id = record["child_session_id"][len("agent-"):] if record["child_session_id"].startswith("agent-") \
        else record["child_session_id"]
    child, meta_path = _build_child_session(
        runtime=runtime, spec=spec, agent_id=agent_id, model_override=tool_input.get("model"),
        parent_tool_use_id=tool_id, background=False,  # a resume always runs in the foreground
    )
    prompt = tool_input.get("prompt") or "Please continue."
    child_events = _run_child_to_completion(child, prompt, agent_id=agent_id, parent_tool_use_id=tool_id,
                                             on_event=on_event)
    text = _final_text_from_log(child)
    _write_meta(meta_path, {"status": "completed"})
    parent.permission_denials.extend(child.permission_denials)  # H6/D10, see run_agent_call's own comment
    wrapped = _wrap_task_result(text, task_id)
    capped = spill_and_truncate(wrapped, cap=RESULT_CAP, session_dir=parent.log.dir / parent.log.session_id,
                                 tool_use_id=tool_id)
    return child_events, ToolResult(capped)

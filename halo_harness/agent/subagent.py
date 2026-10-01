"""halo_harness.agent.subagent -- the Agent/Task tool's real implementation
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

from halo_harness import events
from halo_harness.config.agents_md import AgentSpec, resolve_agent_model

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
    # V2c (H15) roles: the persisted role table (config.json/team.json, or
    # the cost-aware default -- `roles.py::resolve_role_table`, resolved
    # ONCE by whoever builds the top-level Session) and this run's own CLI
    # `--role name=model` overrides -- both threaded down to every child's
    # own `resolve_agent_model` call unchanged (a grandchild can never
    # spawn per MAX_DEPTH, but `_build_child_session` still copies these
    # onto the child's own nested AgentRuntime for consistency).
    role_table: dict = field(default_factory=dict)
    cli_role_overrides: dict = field(default_factory=dict)
    depth: int = 0
    tasks: dict = field(default_factory=dict)        # task_id -> {"child_session_id", "spec_name", "cwd"}
    lock: "threading.Lock" = field(default_factory=threading.Lock)
    # H9 whole-tree review finding 27 (task-map persistence half): flips
    # True the first time `_hydrate_tasks_from_disk` runs for THIS runtime
    # so a resumed session's on-disk `subagents/*.meta.json` files are
    # only ever scanned once, not on every single Agent/TaskStop call.
    tasks_hydrated: bool = False
    # H11b finding 14: agent_id -> the live child `Session` object, for as
    # long as its own call might still be running -- `run_agent_call`/
    # `_resume_task`/`_bg_run` each register their child here and pop it
    # in a `finally` that also calls `child.close_cc()`, so a `cc:` child's
    # own claude subprocess/bridge/socket never outlives its ONE call.
    # `agent/cc_runtime.py`'s own `close_cc()` ALSO walks this (belt-and-
    # suspenders: quitting mid-call must not leave one running).
    live_children: dict = field(default_factory=dict)


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
    from halo_harness.agent.log import SessionLog

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
                          model_override: Optional[str], parent_tool_use_id: str, background: bool = False,
                          role_override: Optional[str] = None):
    """A real `agent.loop.Session` for `spec`: its OWN fresh (or resumed)
    log, a tool subset frozen from the PARENT's own catalog (never grows
    it -- brief B), its `body` as the full system prompt (CLAUDE.md/memory
    per `AgentSpec.skips_claude_md`/`includes_memory`), a resolved model,
    and a permission mode/engine of its own (sharing the parent's rules,
    scoped by `spec.permission_mode` when set)."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.hooks import HookRunner
    from halo_harness.permissions import PermissionEngine

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

    # V2c (H15): the Agent-tool-call's own `role=` argument, when given,
    # overrides this agent's own file/built-in `role:` for just this one
    # call -- see `resolve_agent_model`'s own docstring for the full chain.
    model_ref, model_profile = resolve_agent_model(
        invocation_model=model_override, frontmatter_model=spec.model,
        role_name=(role_override or spec.role), role_table=runtime.role_table,
        cli_role_overrides=runtime.cli_role_overrides,
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
        # H9 whole-tree review finding 10: a BACKGROUND child gets its OWN
        # abort Event instead of sharing the parent's -- sharing meant Esc
        # on a LATER, unrelated FOREGROUND turn (or Ctrl+C on an empty
        # prompt while idle) cut a background child mid-stream with no
        # relationship to what the user was actually looking at (verified:
        # Esc on an unrelated turn interrupted a running background
        # sub-agent). Only `quit()`/`TaskStop(task_id=...)` on THIS
        # specific task may ever set it (see `run_agent_call` below, which
        # stashes it on `runtime.tasks[task_id]["abort_event"]`).
        abort=(parent.abort if not background else threading.Event()),
        # H9: MUST be passed at construction time, not set afterward --
        # `Session.__init__` reads `self.agent_id` synchronously (critical
        # review finding 16: a child never fires the user's own
        # SessionStart hooks, only SubagentStart) before this call even
        # returns. Also namespaces `_permission_waiters` keys (finding 2,
        # see `Session.__init__`'s own docstring on `agent_id`) so this
        # child can never collide with a sibling's live waiter slot in the
        # dict shared with the parent just below.
        agent_id=agent_id,
        # H9 whole-tree review finding 3: share the PARENT's own
        # JobRegistry instead of letting Session's default (a fresh,
        # private one) leave a background Bash job THIS child starts
        # unreachable by the parent's `kill_all()` on quit and its
        # completion notice stranded on a Session object nobody drains
        # again once this call returns -- see loop.py's own `job_registry=`
        # param docstring for the full failure mode this closes.
        job_registry=parent.job_registry,
    )
    if hook_runner is not None:
        hook_runner.prompt_caller = child._call_model_for_hook
    child.interactive = False  # deliberately NEVER True -- see module docstring (AskUserQuestion/ExitPlanMode stay out of scope)
    # H5c finding 8: a live, answerable permission card, only for a
    # FOREGROUND child of an interactive parent -- see module docstring.
    child._subagent_live_asks = bool(parent.interactive) and not background
    child._permission_waiters = parent._permission_waiters
    child.agent_runtime = AgentRuntime(parent=child, agents=runtime.agents, routes=runtime.routes,
                                        role_table=runtime.role_table, cli_role_overrides=runtime.cli_role_overrides,
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


# H9 whole-tree review finding 26: `turn_done` reasons that mean the child's
# run did NOT end normally -- its own last logged assistant text (what
# `_final_text_from_log` returns) may be a stale, mid-thought fragment from
# BEFORE whatever went wrong, never a real, complete report.
#
# "interrupted" is deliberately NOT in this set: an Esc/Ctrl+C-interrupted
# child is a normal, user-INTENDED stop condition (H5c finding 7's own
# tests pin this exactly: the parent's abort-batch pool result for a
# genuinely-started-then-interrupted child is `is_error=False`, matching
# how an interrupted tool call reads elsewhere in this harness), and
# `synthesize_missing_results` already stamps its own text with "[Request
# interrupted by user]" -- truthful on its own, with no need for (and no
# use duplicating) the "did not finish normally" framing this set exists
# for.
_ABNORMAL_TURN_DONE_REASONS = frozenset({"error", "max_turns", "blocked"})


def _child_turn_outcome(child_events: list) -> "tuple[bool, Optional[str]]":
    """`(is_error, reason)` from the LAST `turn_done` event the child
    actually produced (never raises; `reason` is None only if the child
    somehow never yielded one at all -- shouldn't happen for a real
    `child.turn()` run, but this must never crash the parent's turn over
    it either way). H9 whole-tree review finding 26: verified bug -- a
    provider failure partway through a child handed the parent whatever
    "Let me look at X…" text was logged last, presented as if it were the
    finished report, with no signal at all that the child never actually
    finished."""
    # NOTE: every event in `child_events` is already tagged with this ONE
    # child's own `agent_id` (by `_tag()` in `_run_child_to_completion`,
    # before it's ever appended to this list) -- no further agent_id
    # filtering needed or correct here.
    reason = None
    for ev in child_events:
        if getattr(ev, "kind", None) == "turn_done":
            reason = (ev.data or {}).get("reason") if isinstance(ev.data, dict) else None
    return (reason in _ABNORMAL_TURN_DONE_REASONS), reason


def _rollup_child_cost_into_parent(parent, child, *, agent_id: str, since_index: int = 0,
                                    role: Optional[str] = None) -> None:
    """H9 whole-tree review finding 13: a child's usage/cost used to be
    visible ONLY in its own `subagents/agent-<id>.jsonl` -- never reaching
    `parent.cost_meter` (so `--max-budget-usd`, which reads the PARENT's
    own `message_end.cost_usd`, could never trip on sub-agent spend, and
    the TUI's live status-bar total silently excluded it) nor the parent's
    OWN log (so `/stats`/`stats`, which read one session's `log.nodes()`,
    under-reported both tokens and cost). Called once a child's run ends
    (foreground `run_agent_call`, background `_bg_run`, and `_resume_task`
    -- every path that produces real model-call usage).

    `since_index`: a RESUMED child's `child.log.nodes()` includes every
    PRIOR turn's own "usage" nodes too (the log is reloaded from disk) --
    summing the whole file on every resume would roll up the same historic
    tokens again on top of what an earlier call already rolled up. Only
    nodes appended DURING *this* invocation (index `since_index` onward,
    captured by the caller right after `_build_child_session` returns, before
    `_run_child_to_completion` ever runs) are summed -- `child.cost_meter`
    itself is already scoped this way (a fresh CostMeter every Session
    construction, resumed or not), which is what makes `.turns`/`.total_usd`
    below correct as "this invocation's own total", no slicing needed for
    those two.

    `role` (V2c/H15, additive): this call's own resolved role name
    (`tool_input.get("role") or spec.role`, computed once by the caller),
    tagged onto the SAME rolled-up "usage" node `agent_id` already is --
    `telemetry.py`'s per-role aggregation (`stats --roles`) reads it back;
    `None` (every pre-V2c call site, and any role-less agent) logs no
    `role` field at all, exactly as before this parameter existed."""
    if child.cost_meter.turns <= 0:
        return
    combined: dict = {}
    for node in child.log.nodes()[since_index:]:
        if node.get("type") != "usage":
            continue
        usage = node.get("usage") or {}
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens", "reasoning_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                combined[key] = combined.get(key, 0) + value
    child_cost = child.cost_meter.total_usd if child.cost_meter.has_cost_data else None
    # `SessionLog.append_usage` locks internally (safe from any thread on
    # its own); `CostMeter.add_child_total` does not -- both a background
    # child's OWN thread (`_bg_run`) and several FOREGROUND children
    # running concurrently (a real ThreadPoolExecutor batch, per this
    # module's own docstring) can reach this at the same moment, so the
    # `total_usd +=` inside it is guarded with the SAME lock `_bg_run`
    # already uses for every other write to shared parent state.
    parent.log.append_usage(combined, child_cost, agent_id=agent_id, role=role)
    with parent._agent_notices_lock:
        parent.cost_meter.add_child_total(cost_usd=child_cost, has_cost_data=child.cost_meter.has_cost_data,
                                           turns=child.cost_meter.turns)


def _wrap_task_result(text: str, task_id: str) -> str:
    """H6 scope F (OpenCode adopt item): resumable sub-agent tasks -- a
    later `Agent(task_id=..., prompt=...)` resumes the SAME child session
    with its full prior context (via `Session.__init__`'s own resume path,
    reached because `_child_log_paths` preloads the existing log)."""
    return f'<task_result task_id="{task_id}">\n{text}\n</task_result>'


def _hydrate_tasks_from_disk(runtime: AgentRuntime) -> None:
    """H9 whole-tree review finding 27 (task-map persistence half):
    `runtime.tasks` is in-memory only, so a fresh process (a `-c` resume,
    most commonly, but also a brand new launch someone points at an
    existing session id) starts with it EMPTY -- every task_id the model
    remembers from before the restart would otherwise come back "Unknown
    task_id", even though its child log and meta.json are sitting right
    there on disk. Runs at most ONCE per runtime (guarded by
    `tasks_hydrated`, checked/set under `runtime.lock` alongside every
    other mutation of `runtime.tasks`): globs THIS session's own
    `subagents/agent-*.meta.json` -- the exact directory `_child_log_paths`
    writes into, unchanged across a `-c` resume since that reuses the SAME
    session_id/dir -- and reinserts one `runtime.tasks[task_id]` entry per
    file that has one. A meta.json from before this finding was fixed has
    no `task_id` field and is silently skipped; that task is unrecoverable,
    same as before this fix (no regression, just not a NEW capability for
    OLD sessions).

    `abort_event` is always None for a reconstructed entry: the real
    `threading.Event` lived only in the OLD process's memory and died with
    it. That is not a gap needing a workaround, though -- `_bg_run` runs on
    a `daemon=True` thread specifically because a background sub-agent must
    never outlive the process that started it (same rule Bash background
    jobs follow, see `agent/jobs.py`), so a task whose meta.json still says
    `"background": True` after a restart was, definitionally, killed
    mid-flight along with the old process; there is no live thread left
    anywhere to signal, abort or race against. `tools/task_stop.py` checks
    `abort_event is None` and reports it as nothing-to-stop rather than
    claiming a foreground-only task, using the persisted `"background"`
    flag (`reconstructed`/`was_background` below) to word that correctly.
    """
    with runtime.lock:
        if runtime.tasks_hydrated:
            return
        runtime.tasks_hydrated = True
    parent = runtime.parent
    subdir = parent.log.dir / parent.log.session_id / "subagents"
    if not subdir.is_dir():
        return
    for meta_path in sorted(subdir.glob("agent-*.meta.json")):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        task_id = data.get("task_id")
        session_id = data.get("session_id")
        spec_name = data.get("type")
        if not (isinstance(task_id, str) and task_id and isinstance(session_id, str)
                and isinstance(spec_name, str)):
            continue
        with runtime.lock:
            if task_id in runtime.tasks:
                continue  # already known -- e.g. hydration raced a call that just created it
            runtime.tasks[task_id] = {
                "child_session_id": session_id, "spec_name": spec_name, "cwd": str(parent.cwd),
                "abort_event": None, "reconstructed": True, "running_in_process": False,
                "was_background": bool(data.get("background")),
            }


def run_agent_call(*, runtime: AgentRuntime, tool_id: str, tool_input: dict, tool_name: str,
                    on_event=None) -> "tuple[list, object]":
    """`(events, ToolResult)` for ONE Agent/Task tool_use. Never raises --
    every failure mode becomes an `is_error` ToolResult so a sub-agent's
    own bug can never crash the parent's turn. `on_event` (H5c finding 8):
    forwarded to the FOREGROUND child's own `_run_child_to_completion` so
    its events (most importantly `permission_request`) reach a live caller
    the instant they happen -- never used for a background child (see
    `_bg_run`, whose events are not forwarded to any live stream)."""
    from halo_harness.tools.base import ToolResult
    from halo_harness.tools.truncate import spill_and_truncate

    _hydrate_tasks_from_disk(runtime)
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
    # V2c (H15): `Agent(role=...)` overrides this agent's own file/built-in
    # role for just this one call; `role_name` (used below for the cost
    # rollup's own `stats --roles` tag) mirrors exactly what `resolve_agent_
    # model` inside `_build_child_session` resolves the role AGAINST.
    role_override = tool_input.get("role")
    role_name = role_override or spec.role

    agent_id = _new_agent_id()
    child, meta_path = _build_child_session(
        runtime=runtime, spec=spec, agent_id=agent_id, model_override=model_override, parent_tool_use_id=tool_id,
        background=background, role_override=role_override,
    )
    # H9 whole-tree review finding 13: captured BEFORE the child ever makes
    # a model call -- a resumed child's own log already carries every PRIOR
    # invocation's "usage" nodes (reloaded from disk); only nodes appended
    # from here on belong to THIS call. A fresh (non-resumed) child's log
    # is empty at this point, so this is just 0.
    since_index = len(child.log.nodes())
    # H11b finding 14: registered before the child's own turn ever runs --
    # `close_cc()` (this module's `_bg_run`/foreground `finally` below, and
    # agent/cc_runtime.py's own `close_cc()` walking this on the PARENT's
    # quit) needs to find it here even if the child's very first turn
    # never completes normally.
    runtime.live_children[agent_id] = child
    new_task_id = uuid.uuid4().hex[:12]
    with runtime.lock:
        runtime.tasks[new_task_id] = {"child_session_id": child.log.session_id, "spec_name": spec.name,
                                        "cwd": str(parent.cwd),
                                        # H9 whole-tree review finding 10:
                                        # only present (and only meaningful)
                                        # for a background child -- a
                                        # foreground one shares the
                                        # parent's own Event, already
                                        # reachable through the normal Esc
                                        # path. tools/task_stop.py reads
                                        # this to implement
                                        # `TaskStop(task_id=<agent task>)`.
                                        "abort_event": (child.abort if background else None),
                                        # H9 whole-tree review finding 27
                                        # (concurrent-resume half): True from
                                        # here until `_bg_run`'s own
                                        # `finally` flips it back, ALWAYS
                                        # scoped to THIS process (never
                                        # reconstructed as True by
                                        # `_hydrate_tasks_from_disk`, since a
                                        # restart always means the thread
                                        # that would have flipped it is
                                        # gone) -- `_resume_task` refuses a
                                        # resume while this is True instead
                                        # of racing `_bg_run`'s own appends
                                        # to the SAME child log file.
                                        "running_in_process": background}
    # H9 whole-tree review finding 27 (task-map persistence half): the
    # task_id just minted lives ONLY in the in-memory dict updated above --
    # a fresh process (a `-c` resume, most commonly) would never see it and
    # would call every one of the model's own task_ids "Unknown". Persist
    # it (and whether this task is backgrounded) into the SAME meta.json
    # `_build_child_session` already wrote a "status"/"session_id"/"type"
    # for, so `_hydrate_tasks_from_disk` can reconstruct this exact entry
    # (minus `abort_event`, which cannot survive a process restart -- see
    # that function's own docstring) on a later process's first Agent or
    # TaskStop call.
    _write_meta(meta_path, {"task_id": new_task_id, "background": background})

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
            # H9 whole-tree review finding 27 (concurrent-resume half):
            # everything below runs inside try/finally purely so the
            # `finally` can flip `running_in_process` back to False NO
            # MATTER HOW this thread ends, including a bug/exception deep
            # in `_run_child_to_completion` -- leaving it stuck True would
            # permanently refuse any future resume of this task_id, a worse
            # outcome than the race this flag exists to prevent.
            try:
                child_events = _run_child_to_completion(child, prompt, agent_id=agent_id, parent_tool_use_id=tool_id)
                text = _final_text_from_log(child)
                _fire_subagent_hook(child, "SubagentStop")
                _write_meta(meta_path, {"status": "completed"})
                # H9 whole-tree review finding 13: roll this background child's
                # own usage/cost into the parent BEFORE the completion notice
                # is queued, so by the time the parent's next turn (which
                # applies that notice) actually runs, `--max-budget-usd`/
                # `/stats` already reflect it.
                _rollup_child_cost_into_parent(parent, child, agent_id=agent_id, since_index=since_index, role=role_name)
                # H6/D10 (see the foreground path's own comment): merge even
                # for a background sub-agent, under the same lock its own
                # pending-notice append already uses.
                with parent._agent_notices_lock:
                    parent.permission_denials.extend(child.permission_denials)
                # H9 whole-tree review finding 10: `permission_denials` is a
                # print-mode-only channel (the TUI never renders it) -- a
                # background child's own denied tool calls used to be
                # completely invisible in the TUI, which only ever sees this
                # notice text. Same for an interrupted (quit/TaskStop) child:
                # say so plainly instead of presenting whatever partial text
                # it managed as if it were a normal, complete answer.
                status_bits = []
                if child.abort.is_set():
                    status_bits.append("interrupted")
                if child.permission_denials:
                    names = ", ".join(sorted({d.get("tool_name", "?") for d in child.permission_denials}))
                    status_bits.append(f"{len(child.permission_denials)} tool call(s) denied ({names})")
                # H9 whole-tree review finding 26: same treatment as the
                # foreground path -- a provider failure/max_turns also gets
                # flagged here, not just an abort/denial (the two cases
                # `child.abort.is_set()`/`permission_denials` already covered).
                is_error, abnormal_reason = _child_turn_outcome(child_events)
                if abnormal_reason and abnormal_reason not in ("interrupted",):
                    status_bits.append(abnormal_reason)
                status_suffix = f" [{'; '.join(status_bits)}]" if status_bits else ""
                if is_error:
                    text = f"(this sub-agent may not have finished normally)\n{text}"
                notice = (f"[Background sub-agent '{spec.name}' finished (task_id={new_task_id}){status_suffix}]\n"
                          f"{_wrap_task_result(text, new_task_id)}")
                with parent._agent_notices_lock:
                    parent._pending_agent_notices.append(notice)
                del child_events  # collected for parity/debuggability only; not forwarded (parent turn has ended)
            finally:
                # H11b finding 14: this background child's own claude
                # subprocess/bridge/socket (if it ever used cc:) is closed
                # THE MOMENT this call ends, not left running until the
                # whole halo process quits.
                child.close_cc()
                runtime.live_children.pop(agent_id, None)
                with runtime.lock:
                    entry = runtime.tasks.get(new_task_id)
                    if entry is not None:
                        entry["running_in_process"] = False

        threading.Thread(target=_bg_run, daemon=True, name=f"subagent-{agent_id}").start()
        result = ToolResult(
            f"Sub-agent '{spec.name}' started in the background (task_id={new_task_id}). Its result "
            f"will be reported to you as a notice once it finishes -- continue with other work."
        )
        return [start_ev], result

    try:
        child_events = _run_child_to_completion(child, prompt, agent_id=agent_id, parent_tool_use_id=tool_id,
                                                 on_event=on_event)
    finally:
        # H11b finding 14: a foreground child's own claude subprocess/
        # bridge/socket (if it ever used cc:) is closed the moment its ONE
        # call ends -- `_build_child_session` builds a brand new one on
        # every Agent/task_id-resume call, so there is never a reason to
        # keep it alive past this point.
        child.close_cc()
        runtime.live_children.pop(agent_id, None)
    text = _final_text_from_log(child)
    # H9 whole-tree review finding 26: `text` alone can't tell the parent
    # whether the child actually finished normally -- a provider failure/
    # max_turns/interruption partway through used to hand back whatever
    # was logged last with no signal it wasn't a real, complete answer.
    is_error, abnormal_reason = _child_turn_outcome(child_events)
    _fire_subagent_hook(child, "SubagentStop")
    _write_meta(meta_path, {"status": "completed"})
    # H9 whole-tree review finding 13: see the background path's own
    # comment above -- a FOREGROUND child's usage/cost gets the same
    # rollup, just synchronously here instead of at the end of `_bg_run`.
    _rollup_child_cost_into_parent(parent, child, agent_id=agent_id, since_index=since_index, role=role_name)
    # H6 known v1 gap (D10) / B must-do: a non-interactive child's own
    # "ask" denials (agent/loop.py's `_resolve_tool_call`) land in the
    # CHILD's own `permission_denials` list, which nothing outside this
    # function would otherwise ever read -- merged into the PARENT's here
    # so print mode's top-level result (headless.py reads `session.
    # permission_denials`, the TOP session only) actually surfaces a
    # sub-agent's denied tool calls instead of silently losing them.
    parent.permission_denials.extend(child.permission_denials)
    end_ev = events.Event("subagent_end", {"agent_id": agent_id, "name": spec.name, "parent_tool_use_id": tool_id,
                                             "task_id": new_task_id, "is_error": is_error})
    end_ev.agent_id = agent_id
    if on_event is not None:
        on_event(end_ev)  # H5c finding 8: live too, same reasoning as start_ev above

    if is_error:
        text = f"[sub-agent did not finish normally ({abnormal_reason}) -- this may be a stale/partial answer]\n{text}"
    wrapped = _wrap_task_result(text, new_task_id)
    capped = spill_and_truncate(wrapped, cap=RESULT_CAP, session_dir=parent.log.dir / parent.log.session_id,
                                 tool_use_id=tool_id)
    return [start_ev, *child_events, end_ev], ToolResult(capped, is_error=is_error)


def _resume_task(runtime: AgentRuntime, task_id: str, tool_input: dict, tool_id: str, *, on_event=None):
    from halo_harness.tools.base import ToolResult
    from halo_harness.tools.truncate import spill_and_truncate

    parent = runtime.parent
    record = runtime.tasks.get(task_id)
    if record is None:
        return [], ToolResult(f"Unknown task_id {task_id!r} -- it may belong to a different session.", is_error=True)
    # H9 whole-tree review finding 27 (concurrent-resume half): a
    # background child still has its OWN `_bg_run` thread appending to
    # this exact task_id's child log/meta.json -- resuming NOW would build
    # a second, independent Session on top of the same files and both
    # would write to them at once. `running_in_process` is per-PROCESS
    # in-memory state (see its own comment where it's set), so this can
    # never be a stale True surviving a `-c` restart -- a reconstructed
    # task (`record.get("reconstructed")`) is never True here, because the
    # thread that would have set it died with the old process.
    if record.get("running_in_process"):
        return [], ToolResult(
            f"Task {task_id!r} is still running in the background -- resuming it now would race its own "
            f"in-progress work. Wait for its completion notice before sending it another prompt.",
            is_error=True,
        )
    spec = runtime.agents.get(record["spec_name"])
    if spec is None:
        return [], ToolResult(f"The agent type for task_id {task_id!r} is no longer available.", is_error=True)

    agent_id = record["child_session_id"][len("agent-"):] if record["child_session_id"].startswith("agent-") \
        else record["child_session_id"]
    # V2c (H15): a resume may itself carry a fresh `role=` override; else
    # this resumed call keeps resolving against the SAME agent's own role.
    role_override = tool_input.get("role")
    role_name = role_override or spec.role
    child, meta_path = _build_child_session(
        runtime=runtime, spec=spec, agent_id=agent_id, model_override=tool_input.get("model"),
        parent_tool_use_id=tool_id, background=False,  # a resume always runs in the foreground
        role_override=role_override,
    )
    # H9 whole-tree review finding 13: see run_agent_call's own comment on
    # its identically-named local -- a resume's own child log already
    # carries every PRIOR call's usage nodes; only what's appended from
    # here on is THIS call's own new spend.
    since_index = len(child.log.nodes())
    runtime.live_children[agent_id] = child  # H11b finding 14, see run_agent_call's own comment
    prompt = tool_input.get("prompt") or "Please continue."
    try:
        child_events = _run_child_to_completion(child, prompt, agent_id=agent_id, parent_tool_use_id=tool_id,
                                                 on_event=on_event)
    finally:
        child.close_cc()
        runtime.live_children.pop(agent_id, None)
    text = _final_text_from_log(child)
    # H9 whole-tree review finding 26: same treatment as run_agent_call's
    # own foreground path -- see its comment.
    is_error, abnormal_reason = _child_turn_outcome(child_events)
    _write_meta(meta_path, {"status": "completed"})
    _rollup_child_cost_into_parent(parent, child, agent_id=agent_id, since_index=since_index, role=role_name)
    parent.permission_denials.extend(child.permission_denials)  # H6/D10, see run_agent_call's own comment
    if is_error:
        text = f"[sub-agent did not finish normally ({abnormal_reason}) -- this may be a stale/partial answer]\n{text}"
    wrapped = _wrap_task_result(text, task_id)
    capped = spill_and_truncate(wrapped, cap=RESULT_CAP, session_dir=parent.log.dir / parent.log.session_id,
                                 tool_use_id=tool_id)
    return child_events, ToolResult(capped, is_error=is_error)

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
import logging
import subprocess
import threading
import time
import uuid
import contextlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from halo_harness import events

log = logging.getLogger("bridge")
from halo_harness.config.agents_md import AgentSpec, resolve_agent_model

MAX_CONCURRENT_AGENTS = 4
MAX_DEPTH = 1
RESULT_CAP = 30_000


def effective_max_concurrent(runtime: "AgentRuntime") -> int:
    """Halo 2.0.2 round 2 (brief B): "concurrency from `agents.max_
    concurrent` (config, default 4) unless the org sets `max_concurrent`"
    -- an org's own value (threaded onto its root `AgentRuntime` by
    `run_org_call`, copied to every descendant by `_build_child_session`)
    wins outright; otherwise the `agents.max_concurrent` config knob,
    defaulting to the original hardcoded `MAX_CONCURRENT_AGENTS` so every
    pre-2.0.2-round-2 session behaves exactly as before. Never raises: a
    bad config/org value (not an int, <= 0) falls back to that same
    default."""
    candidate = runtime.max_concurrent
    if candidate is None:
        from halo_harness.theme import get_config_value
        candidate = get_config_value("agents.max_concurrent", default=MAX_CONCURRENT_AGENTS)
    try:
        value = int(candidate)
        return value if value > 0 else MAX_CONCURRENT_AGENTS
    except (TypeError, ValueError):
        return MAX_CONCURRENT_AGENTS


def effective_max_depth(runtime: "AgentRuntime") -> int:
    """Halo 2.0.2 round 3 (brief C): "agents.max_depth (default 1, up to
    3)" -- mirrors `effective_max_concurrent`'s own shape exactly. An
    org's own tree-derived `max_depth` (set on its root `AgentRuntime` by
    `run_org_call`, "depth comes from the tree") wins outright, same as
    an org's `max_concurrent` already does -- NOT clamped to 3, since a
    legitimate org tree (e.g. the `company` built-in: CEO -> VP -> manager
    -> worker) is routinely deeper than the config knob's own range.
    Only the CONFIG-sourced value (every non-org session) is clamped to
    [1, 3]; a bad/missing/out-of-range one falls back to the original
    hardcoded `MAX_DEPTH=1` so every pre-2.0.2-round-3 session behaves
    exactly as before this knob existed. Never raises."""
    if runtime.max_depth is not None:
        try:
            value = int(runtime.max_depth)
            return value if value > 0 else MAX_DEPTH
        except (TypeError, ValueError):
            return MAX_DEPTH
    from halo_harness.theme import get_config_value
    candidate = get_config_value("agents.max_depth", default=MAX_DEPTH)
    try:
        value = int(candidate)
    except (TypeError, ValueError):
        return MAX_DEPTH
    return value if 1 <= value <= 3 else MAX_DEPTH


class SessionConcurrencyGate:
    """2.0.2 review finding 10 (major): a bounded gate shared by
    reference down the WHOLE tree (`AgentRuntime.concurrency_semaphore`,
    propagated by `_build_child_session`/`run_org_call` exactly like
    `org_budget` already is), acquired around each child's own run in
    EVERY spawn path -- single, fan-out, resume -- so the session-/org-
    wide concurrency cap is real: two separate `count` calls in one turn
    (or nested levels) can no longer multiply past it just because each
    one's own `ThreadPoolExecutor` pool is independently sized.

    Deliberately NOT a bare `threading.Semaphore`: a child delegating to
    a grandchild is logically one more hop of the SAME call already
    holding a slot, but PHYSICALLY it runs on a brand new pool-worker
    thread -- even a lone Agent tool_use goes through `agent/loop.py`'s
    `_run_agent_batch`, which always builds its own `ThreadPoolExecutor`
    (`max_workers=min(cap, 1)` for exactly one call). A bare semaphore
    sized below the nesting depth deadlocks: the OUTER thread blocks
    waiting (`queue.Queue.get()`) for the inner pool's worker to finish,
    while that worker blocks waiting for the very slot the outer thread
    is holding. Verified directly (a traced plain-semaphore substitute
    produced exactly this hang on a 2-level chain at cap=1).

    Fixed with EXPLICIT nesting-depth handoff across the thread
    boundary: `wrap_for_pool`, called on the SUBMITTING thread right
    before `pool.submit`, captures that thread's current depth and
    returns a wrapper which seeds the SAME depth onto whichever worker
    thread the pool actually runs it on (restored afterward, since pool
    threads are reused across unrelated later jobs). A plain `threading.
    local` alone cannot do this -- a new thread never inherits another
    thread's local storage, pool-reused or not."""

    def __init__(self, value: int) -> None:
        # A counter under a Condition rather than a Semaphore, so the cap
        # can change while slots are held: `agents.max_concurrent` is read
        # again at every spawn (`set_limit`), which is how a config change
        # during a session applies to the next spawn instead of the next
        # session (the test that found this wrote the config after the
        # Session was built, exactly what `/setup` does).
        self._limit = max(1, int(value))
        self._active = 0
        self._cv = threading.Condition()
        self._local = threading.local()

    def set_limit(self, value) -> None:
        try:
            limit = max(1, int(value))
        except (TypeError, ValueError):
            return
        with self._cv:
            self._limit = limit
            self._cv.notify_all()

    @property
    def limit(self) -> int:
        return self._limit

    def _acquire(self) -> None:
        with self._cv:
            while self._active >= self._limit:
                self._cv.wait()
            self._active += 1

    def _release(self) -> None:
        with self._cv:
            self._active = max(0, self._active - 1)
            self._cv.notify_all()

    def __enter__(self) -> "SessionConcurrencyGate":
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            self._acquire()
        self._local.depth = depth + 1
        return self

    def __exit__(self, *exc_info) -> bool:
        self._local.depth -= 1
        if self._local.depth == 0:
            self._release()
        return False

    def wrap_for_pool(self, fn: Callable) -> Callable:
        """Call on the thread about to `pool.submit(...)` a job that may
        itself (directly or several Agent-dispatch hops later) try to
        enter THIS gate again -- never on the pool worker thread itself.
        The returned callable, run on whatever thread the pool actually
        picks, inherits this submitting thread's CURRENT depth for its
        own duration, then restores whatever depth that worker thread
        had before (0 for a fresh one; otherwise whatever an EARLIER,
        unrelated job left it at -- pool threads are reused)."""
        # Fix pass A, found by the session-wide test on all three platforms:
        # seeding the worker with the SUBMITTING thread's depth let every
        # fan-out child skip the semaphore whenever the submitter already
        # held a slot (two `batch` calls, cap 2, peaked at 4). A pool worker
        # now starts OUTSIDE the gate and takes a real slot; the submitting
        # thread gives its own slot back for as long as it waits on the
        # pool (`released()` / `pool()` below), which is what keeps a
        # nested chain at cap 1 from deadlocking: a parent blocked on its
        # children is not running.
        inherited = 0

        def _wrapped(*args, **kwargs):
            prev = getattr(self._local, "depth", 0)
            self._local.depth = inherited
            try:
                return fn(*args, **kwargs)
            finally:
                self._local.depth = prev
        return _wrapped

    @contextlib.contextmanager
    def released(self):
        """Give this thread's slot back while it blocks on children, and
        take it again afterwards (a no-op for a thread outside the gate).
        The per-thread depth is kept as it is, so the thread is still
        "inside" the gate logically and its own `__exit__` still balances."""
        depth = getattr(self._local, "depth", 0)
        if depth > 0:
            self._release()
        try:
            yield
        finally:
            if depth > 0:
                self._acquire()

    def pool(self, max_workers: int, limit: "Optional[int]" = None) -> "_GatedPool":
        """A `ThreadPoolExecutor` stand-in for the two spawn sites: the
        submitting thread's slot is released for the whole `with` block
        and re-taken on exit, every submitted job runs outside the gate so
        its own `with gate:` acquires a real slot, and `limit` (the cap
        read from config or the org right now) resizes the gate first."""
        if limit is not None:
            self.set_limit(limit)
        return _GatedPool(self, max_workers)


class _GatedPool:
    def __init__(self, gate: "SessionConcurrencyGate", max_workers: int) -> None:
        self._gate = gate
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._released = None

    def __enter__(self) -> "_GatedPool":
        self._released = self._gate.released()
        self._released.__enter__()
        return self

    def submit(self, fn: Callable, *args, **kwargs):
        return self._executor.submit(self._gate.wrap_for_pool(fn), *args, **kwargs)

    def __exit__(self, *exc_info) -> bool:
        try:
            self._executor.shutdown(wait=True)
        finally:
            self._released.__exit__(*exc_info)
        return False


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
    # Halo 2.0.2 round 2 (brief B): an organization run's own depth cap
    # ("depth comes from the tree" -- `orgs.org_tree_depth(org) + 1`, set
    # by `run_org_call` below) and concurrency cap (the org's own `max_
    # concurrent`, or left `None` to fall through to the `agents.max_
    # concurrent` config knob -- `effective_max_concurrent` below). `None`
    # for every non-org caller (unchanged: `MAX_DEPTH`/the config knob
    # apply exactly as before this existed). `_build_child_session` copies
    # both onto every descendant's own nested `AgentRuntime` so a WHOLE
    # org tree -- not just its root -- shares the same two caps.
    max_depth: Optional[int] = None
    max_concurrent: Optional[int] = None
    # Halo 2.0.2 round 7 (init wizard brief 3b): an organization run's own
    # budget tracker (`orgs.OrgBudgetTracker`, untyped here for the same
    # reverse-import reason `role_table`/`routes` already are plain
    # dicts) -- `None` for every non-org caller. Shared BY REFERENCE down
    # the whole tree (copied onto every descendant's own nested
    # AgentRuntime by `_build_child_session`, exactly like `tasks`/`lock`
    # already are), never rebuilt per hop, so one org-wide/per-position
    # spend total survives the whole run, not just one level of it.
    org_budget: Optional[object] = None
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
    # H9 whole-tree review finding 10: ONE semaphore shared by reference
    # down the WHOLE tree from wherever it's first sized for real (`agent.
    # loop.Session.__init__` for an ordinary session, `run_org_call` for an
    # org run -- both below), acquired around each child's own run in
    # EVERY spawn path (single, fan-out, resume) so the session- (or org-)
    # wide concurrency cap is real: two separate `count` calls, or nested
    # levels, can no longer multiply past it just because each one's own
    # ThreadPoolExecutor pool is independently sized. Defaults to an
    # effectively-unbounded semaphore so a bare `AgentRuntime(parent=...)`
    # built directly (every pre-existing test fixture, never routed
    # through either real construction site) behaves exactly as before
    # this field existed -- unlimited, never a surprise new cap in a test
    # that never asked for one.
    concurrency_semaphore: "object" = field(default_factory=lambda: SessionConcurrencyGate(1_000_000))


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
                          role_override: Optional[str] = None, effort_override: Optional[str] = None):
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

    # W4a misc: `isolation: worktree` frontmatter -- "shared with
    # --worktree" (the same helper `headless.run_print_mode` uses for the
    # CLI flag). Falls back to the parent's own cwd (one log line, never a
    # hard failure) when this isn't a git repo or the worktree creation
    # itself fails -- a sub-agent must still run even when isolation isn't
    # available.
    child_cwd = parent.cwd
    # finding 14 (W6a): the worktree path THIS call created (None when
    # isolation isn't requested, or creation fell back to the parent's
    # own cwd) -- stashed on `child` below so `run_agent_call`'s own
    # completion handling (both the foreground and background paths) can
    # remove a CLEAN tree (firing WorktreeRemoved) or surface a DIRTY
    # one's path/branch in the Agent result instead of silently
    # stranding it under `~/.halo/worktrees`.
    isolation_worktree_path = None
    isolation_worktree_branch = None
    if spec.isolation == "worktree":
        from halo_harness.worktree import create_worktree
        wt_path, wt_error = create_worktree(parent.cwd, f"agent-{agent_id}", state_dir=parent.state_dir)
        if wt_path is not None:
            child_cwd = wt_path
            isolation_worktree_path = wt_path
            try:
                branch_result = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(wt_path),
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                )
                if branch_result.returncode == 0:
                    isolation_worktree_branch = branch_result.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            log.warning("sub-agent %s isolation:worktree could not create a worktree (%s) -- "
                        "running in the parent's own cwd instead", spec.name, wt_error)

    catalog_names = (parent.session_catalog.names if parent.session_catalog is not None
                      else parent.tool_registry.names())
    tool_names = spec.resolved_tools(catalog_names)
    child_registry = parent.tool_registry.filtered(tool_names)

    ctx = SessionContext(
        cwd=child_cwd, model_label=parent.model_label, model_family="generic",
        bare=spec.skips_claude_md(), tool_registry=child_registry, mcp_servers=None,
    )
    ctx.system_prompt = spec.body or spec.description
    # Parity gap: agent frontmatter `memory:` used to be a bare truthy
    # gate -- ANY non-empty value always resolved to the SAME global
    # `~/.halo/agents/<name>/` directory, never created it, and (for that
    # global directory specifically) left it OUTSIDE the child's own
    # permission working dirs (asked for in default mode, flatly denied
    # in -p/background, for every Read/Write/Edit the agent made against
    # the exact path its own system prompt told it to use). Now honours
    # the actual scope value -- "user" (global, unchanged path; also the
    # fallback for a bare truthy non-string value like YAML `memory:
    # true`, or any unrecognized string, so nothing that used to work
    # silently stops), "project" (inside THIS project's own cwd, so it is
    # already a working dir with no extra_dirs change needed at all) or
    # "local" (this project only, but kept out of the project tree
    # itself, under state_dir keyed by a project slug) -- a PLAUSIBLE
    # mirror of Claude Code's own user|project|local agent-memory scopes,
    # never verified against a real Claude Code agent definition.
    memory_scope = spec.memory if isinstance(spec.memory, str) and spec.memory.strip() else (
        "user" if spec.memory else None)
    memory_extra_dir: Optional[Path] = None
    if memory_scope:
        if memory_scope == "project":
            memory_dir = Path(parent.cwd) / ".halo" / "agents" / spec.name
        elif memory_scope == "local":
            from halo_harness.config.paths import project_slug
            memory_dir = Path(parent.state_dir) / "agents-local" / project_slug(parent.cwd) / spec.name
            memory_extra_dir = memory_dir
        else:  # "user", or any other/unrecognized value -- the original, global default
            memory_dir = Path(parent.state_dir) / "agents" / spec.name
            memory_extra_dir = memory_dir
        memory_file = memory_dir / "MEMORY.md"
        try:
            memory_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        ctx.system_prompt = (
            f"{ctx.system_prompt}\n\nYour persistent memory directory is {memory_dir} -- write notes there "
            f"(e.g. {memory_file.name}) that you want available to your NEXT invocation; its current "
            f"contents, if any, are included below as a snapshot."
        )

        def _read_agent_memory(path=memory_file) -> str:
            try:
                return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            except OSError:
                return ""

        ctx.memory_snapshot_text = _read_agent_memory
    elif not spec.includes_memory():
        ctx.memory_snapshot_text = lambda: ""  # D-CFG: "no memory index unless general-purpose"

    # V2c (H15): the Agent-tool-call's own `role=` argument, when given,
    # overrides this agent's own file/built-in `role:` for just this one
    # call -- see `resolve_agent_model`'s own docstring for the full chain.
    _effective_role_name = role_override or spec.role
    model_ref, model_profile = resolve_agent_model(
        invocation_model=model_override, frontmatter_model=spec.model,
        role_name=_effective_role_name, role_table=runtime.role_table,
        cli_role_overrides=runtime.cli_role_overrides,
        env=parent.tool_env, settings=getattr(parent.session_context, "settings", None),
        parent_ref=parent.model_ref, parent_profile=parent.model_profile,
        parent_small_ref=parent.small_model_ref, state_dir=parent.state_dir, routes=runtime.routes,
    )
    # 2.0.2 review finding 7 (major): a decision-only/judge endpoint
    # (`Controller.set_model` auto-routes one to `roles.judge` rather than
    # ever installing it as the session model) can never carry the
    # built-in `Judge` agent's own HARD-CODED Explore tool set -- every
    # request it made raised `ToolsNotSupported` before the model ever
    # got to answer, and the user-facing error pointed back at "use the
    # judge role instead of the session model", which was already in use.
    # Tool-less instead whenever the RESOLVED model itself can't take
    # tools, regardless of which agent/role asked for it -- the registry
    # empties out (same shape `--tools ""` already supports) and the
    # prompt gets an explicit heads-up to decide from the text alone.
    # `model_profile` here is `halo_harness.model.ModelProfile` (context/
    # pricing/vision only -- no `tools_supported` field at all); decision-
    # only-ness is a `providers.profiles` concept, the SAME check
    # `Controller.set_model`/`headless.build_session` already make before
    # ever letting this ref become the session model.
    if model_ref.provider == "databricks":
        from halo_harness.providers.profiles import decision_only_notice
        if decision_only_notice(model_ref.model):
            ctx.tool_registry = parent.tool_registry.filtered([])
            ctx.system_prompt = (f"{ctx.system_prompt}\n\nThis endpoint supports no tool calls at all -- decide "
                                  f"and answer directly from the prompt text alone.")
    # Halo 2.0.2 (brief A.2): "sub-agent runs apply the role's effort
    # through the same path the session uses" -- resolved alongside (not
    # inside) `resolve_agent_model` so that function's return shape never
    # changes; applied below exactly where `spec.effort` already was,
    # one rung below it (an agent file's own explicit `effort:` still
    # wins) and one rung above blindly inheriting the parent's.
    from halo_harness.roles import role_effort_for
    _role_effort = role_effort_for(_effective_role_name, role_table=runtime.role_table,
                                    cli_overrides=runtime.cli_role_overrides)
    # Halo 2.0.2 round 3 (brief C): a `count`/`batch` fan-out job's own
    # per-item `effort` override (`_run_agent_fanout` below) -- one rung
    # ABOVE everything else here, same precedence `role_override`/`model_
    # override` already get over this agent's own file/role defaults.
    _effective_effort = effort_override or spec.effort or _role_effort or parent.effort

    permission_mode = spec.permission_mode or parent.permission_engine.mode
    # finding 14 (W6a): both of these used to root at `parent.cwd`
    # unconditionally -- for an `isolation: worktree` child (`child_cwd`
    # above, the worktree path), every edit landed OUTSIDE the engine's
    # own working directory: asked for in default/acceptEdits mode, and
    # flatly denied in print/background mode, no matter what the child
    # actually did.
    # Parity gap: the "user"/"local" memory scopes above resolve OUTSIDE
    # `child_cwd` (by design -- global or state-dir-rooted) -- added here
    # so Read/Write/Edit against the agent's own documented memory path
    # never has to ask (default mode) or get flatly denied (-p/
    # background) just because it is not literally inside the cwd.
    # `PermissionEngine.__init__` already copies this into its OWN new
    # list, so appending here never mutates the PARENT's own extra_dirs.
    child_extra_dirs = list(parent.permission_engine.extra_dirs)
    if memory_extra_dir is not None:
        child_extra_dirs.append(memory_extra_dir)
    child_engine = PermissionEngine(
        deny_rules=parent.permission_engine.deny_rules, ask_rules=parent.permission_engine.ask_rules,
        allow_rules=list(parent.permission_engine.allow_rules), mode=permission_mode,
        cwd=child_cwd, extra_dirs=child_extra_dirs,
        print_mode=parent.permission_engine.print_mode,
    )

    # Parity gap: `Session.plugin_roots` reads `cli_flags["resolved_
    # plugin_roots"]` -- a child's own `cli_flags` dict below used to
    # carry only the worktree key, so `self.plugin_roots` was always `[]`
    # for every sub-agent regardless of what the PARENT session's own
    # `--plugin-dir`/`--plugin-url` roots were, and a model-invoked Skill
    # tool call inside that sub-agent could never find one of those
    # skills at all (`tools/skill.py` reads `ctx.plugin_roots`, itself
    # read from `Session.plugin_roots`). Every OTHER cli_flags-driven
    # behavior still stays off for a child, unchanged.
    _child_cli_flags: dict = {}
    if parent.plugin_roots:
        _child_cli_flags["resolved_plugin_roots"] = parent.plugin_roots

    hook_runner = None
    if parent.hook_runner is not None:
        hook_runner = HookRunner(
            parent.hook_runner.hooks_by_event, cwd=child_cwd, session_id=child_log.session_id,
            transcript_path=str(child_log.path), effective_env=parent.hook_runner.effective_env,
            permission_mode=permission_mode, effort=_effective_effort,
            mcp_manager=parent.mcp_manager, prompt_caller=parent.hook_runner.prompt_caller,
            enabled=parent.hook_runner.enabled, agent_id=agent_id, agent_type=spec.name,
        )

    child = Session(
        cwd=child_cwd, model_ref=model_ref, model_profile=model_profile, creds=parent.creds,
        state_dir=parent.state_dir, model_label=model_ref.raw, session_context=ctx,
        small_model_ref=parent.small_model_ref, small_model_effort=getattr(parent, "small_model_effort", None),
        session_log=child_log,
        max_turns=(spec.max_turns or parent.max_turns), effort=_effective_effort,
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
        # finding 14 (W6a): the ONLY purpose of this is so `Session.
        # __init__`'s own (already-built, already-tested) `_fire_
        # worktree_created` fires for THIS child too -- it reads exactly
        # this key and no other (a child never gets cli_flags otherwise;
        # every OTHER cli_flags-driven behavior stays off, unchanged).
        cli_flags=({**_child_cli_flags, "_worktree_created_path": str(isolation_worktree_path)}
                    if isolation_worktree_path is not None else (_child_cli_flags or None)),
        # Halo 2.0.2 round 2 (brief B): an organization position's own
        # `reports` (`AgentSpec.delegate_restriction`) becomes THIS
        # child's own `agent_type_restriction` -- the SAME check `run_
        # agent_call` below already enforces for a `tools: ["Agent(name)"]`
        # content-restricted agent file, now actually reaching a child
        # (previously set only on the TOP-level session by headless.py;
        # no in-tree caller ever passed it down before this). `None` for
        # every non-org spec, unchanged.
        agent_type_restriction=spec.delegate_restriction,
    )
    if hook_runner is not None:
        hook_runner.prompt_caller = child._call_model_for_hook
    # finding 14 (W6a): read back by `run_agent_call`'s own completion
    # handling (foreground and background alike) once this child's run
    # ends, to remove a CLEAN tree or surface a DIRTY one's path/branch.
    child._isolation_worktree_path = isolation_worktree_path
    child._isolation_worktree_branch = isolation_worktree_branch
    child._isolation_worktree_repo_root = parent.cwd
    child.interactive = False  # deliberately NEVER True -- ExitPlanMode stays out of scope (see module docstring)
    # H5c finding 8 / W4a: a live, answerable permission card (and, as of
    # W4a, AskUserQuestion too) for ANY child of an interactive parent,
    # foreground OR background -- `_run_child_to_completion`'s `on_event`
    # forwards a foreground child's events into the parent's own live `turn()`
    # stream; `_bg_run` below forwards a BACKGROUND child's through `parent.
    # _event_sink` directly instead (there is no live `turn()` consumer for
    # it to ride along with). `_question_waiters` is shared exactly like
    # `_permission_waiters` already was -- both namespaced by agent_id in
    # `_resolve_tool_call` for the same reason (parallel/sequential children's
    # own tool_use ids can collide).
    child._subagent_live_asks = bool(parent.interactive)
    child._permission_waiters = parent._permission_waiters
    child._question_waiters = parent._question_waiters
    child.agent_runtime = AgentRuntime(parent=child, agents=runtime.agents, routes=runtime.routes,
                                        role_table=runtime.role_table, cli_role_overrides=runtime.cli_role_overrides,
                                        depth=runtime.depth + 1, tasks=runtime.tasks, lock=runtime.lock,
                                        # Halo 2.0.2 round 2 (brief B): propagated to EVERY descendant, not
                                        # just this one hop -- an org's depth/concurrency caps must survive
                                        # the whole tree, not reset to the module defaults one level down.
                                        max_depth=runtime.max_depth, max_concurrent=runtime.max_concurrent,
                                        # round 7 (brief 3b): the SAME tracker object, never a fresh one --
                                        # see AgentRuntime.org_budget's own docstring.
                                        org_budget=runtime.org_budget,
                                        # finding 10: the SAME semaphore object, never a fresh one -- see
                                        # AgentRuntime.concurrency_semaphore's own docstring.
                                        concurrency_semaphore=runtime.concurrency_semaphore)
    _write_meta(meta_path, {
        "agent_id": agent_id, "type": spec.name, "description": spec.description,
        "model": model_ref.raw, "parent_tool_use_id": parent_tool_use_id,
        "started": time.time(), "status": "running", "session_id": child_log.session_id,
    })
    return child, meta_path


def _fire_task_hook(parent, event_name: str, *, task_id: str, spec_name: str, description: str) -> None:
    """W4a: TaskCreated/TaskCompleted -- "background jobs and sub-agents"
    (removed from NOT_EMITTED_V1). Fires on the PARENT's own hook_runner
    (never the child's, unlike SubagentStart/Stop) -- a task_id is the
    PARENT's own bookkeeping concept (`runtime.tasks`), so this is the
    session whose hooks should see it created/completed. `agent/jobs.py`'s
    background Bash jobs fire the SAME two events from their own call
    sites for the other half of "background jobs and sub-agents"."""
    if parent.hook_runner is None or not parent.hook_runner.has_hooks(event_name):
        return
    payload = parent.hook_runner.payload(event_name, extra={
        "task_id": task_id, "subagent_type": spec_name, "description": description,
    })
    try:
        parent._run_hook(event_name, payload, matched=spec_name)
    except Exception:
        pass


def _fire_subagent_hook(child, event_name: str) -> None:
    """W3b item 11: `child._run_hook`/`_run_hook_stop` (never the bare
    `child.hook_runner.run(...)` this used to call directly) so a child's
    own SubagentStart/SubagentStop lands in ITS OWN per-turn timeline
    (`child._timeline`, never the parent's or the shared module-level
    default) -- the exact per-session isolation this round's own gap list
    asks for."""
    if child.hook_runner is None:
        return
    try:
        if event_name == "SubagentStop":
            child._run_hook_stop("SubagentStop")
        else:
            child._run_hook(event_name, child.hook_runner.payload(event_name))
    except Exception:
        pass


def _tag(ev, *, agent_id: str, parent_tool_use_id: str):
    """Finding 6 (major): a delegate's own events (a VP's under a CEO, in
    a depth > 1 org) used to have their `agent_id` OVERWRITTEN at every
    level they bubble up through -- each ancestor's own `_run_child_to_
    completion` called this with ITS OWN `agent_id`, unconditionally,
    even for an event a DEEPER call already tagged with the real
    originator's id. Verified: `_tag(Event(agent_id="vp000001"),
    agent_id="ceo00001")` gave `agent_id == "ceo00001"` while `ev.data.
    agent_id` stayed `"vp000001"` -- the dock keys cards by `ev.agent_id`,
    so the VP's own card got the CEO's key, orphaning the CEO's real card
    (never finished, ticks "thinking Ns" forever) and misrouting the VP's
    text into it. Only set when not already set, so the DEEPEST
    (innermost/real) agent_id survives every hop back up -- `root_agent_
    id` records the outermost hop's own id for anything that wants it."""
    if not getattr(ev, "agent_id", None):
        ev.agent_id = agent_id
    if isinstance(ev.data, dict):
        extra = {"parent_tool_use_id": parent_tool_use_id}
        if "root_agent_id" not in ev.data:
            extra["root_agent_id"] = agent_id
        ev.data = {**ev.data, **extra}
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


def _finalize_child_isolation_worktree(child) -> "Optional[str]":
    """finding 14 (W6a): the other half of `isolation: worktree` -- called
    once THIS child's run has fully ended (foreground and background
    alike). A no-op (`None`) when this child never got its own worktree
    (`_isolation_worktree_path` is None -- isolation wasn't requested, or
    creation fell back to the parent's own cwd). A CLEAN tree (`git
    status` empty) is removed -- firing WorktreeRemoved -- and its own
    `halo-worktree-*` branch deleted with it; a DIRTY one (the point of
    isolating it in the first place, most of the time) is left alone, and
    this returns a note naming its path/branch for the caller to append
    to the Agent result text, so the child's own work is never silently
    stranded under `~/.halo/worktrees` with no way for the user to find
    it again. Never raises."""
    wt_path = getattr(child, "_isolation_worktree_path", None)
    if wt_path is None:
        return None
    branch = getattr(child, "_isolation_worktree_branch", None)
    repo_root = getattr(child, "_isolation_worktree_repo_root", None) or wt_path.parent
    try:
        from halo_harness.shadow import git_status_dirty_paths
        dirty = git_status_dirty_paths(wt_path)
    except Exception:
        dirty = None
    if dirty:  # a real, non-empty set of uncommitted changes
        return (f"(this sub-agent's isolated worktree has uncommitted changes and was kept at "
                f"{wt_path}{f' on branch {branch}' if branch else ''} -- remove it yourself with "
                f"`halo worktree rm {wt_path}` once you're done with it)")
    if dirty is None:  # could not determine (not a repo somehow, git unreachable, ...) -- never guess
        return f"(this sub-agent's isolated worktree at {wt_path} was kept -- its status could not be checked)"
    try:
        from halo_harness.worktree import remove_worktree
        # review finding 28: `remove_worktree` now deletes the worktree's
        # own branch itself, right after a successful removal -- the
        # separate `git branch -D` this used to run again here (after
        # already passing its OWN upfront dirty check above, before
        # `remove_worktree` grew the identical check internally) is gone;
        # `_reason` is only ever "dirty"/"failed" when `removed` is False,
        # already covered above/below.
        removed, _reason = remove_worktree(wt_path, repo_root_hint=repo_root)
    except Exception:
        removed = False
    if removed:
        try:
            child._fire_worktree_removed(wt_path)
        except Exception:
            pass
    return None


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


def _invalid_role_error(role: "Optional[str]") -> "Optional[str]":
    """2.0.2 review finding 22: `None` when `role` is unset or a KNOWN
    role name; otherwise an error string listing the known roles. Shared
    by every `Agent(role=...)` surface (single call, batch/count fan-out
    items, and task resume) -- none of them checked this before, so a
    typo'd role silently fell through to resolving against the session
    model instead of erroring the way an unknown `subagent_type` already
    does."""
    if not role:
        return None
    from halo_harness.roles import known_role_names
    known = known_role_names()
    if role in known:
        return None
    return f"Unknown role {role!r}. Known roles: {', '.join(known)}"


def _prepare_fanout_jobs(spec: AgentSpec, tool_input: dict) -> "tuple[Optional[list], Optional[str]]":
    """Parses `count`/`batch` into a list of per-job dicts (`prompt`,
    `role`, `model`, `effort`, `description`) -- `(jobs, None)` on
    success, `(None, error_text)` on a bad shape. The caller only reaches
    this when at least one of `count`/`batch` is not None; `batch` wins
    if somehow both were given."""
    description = tool_input.get("description") or spec.description[:60]
    batch = tool_input.get("batch")
    if batch is not None:
        if not isinstance(batch, list) or not batch:
            return None, "batch must be a non-empty list of {prompt, role?, model?, effort?} objects"
        jobs = []
        for i, item in enumerate(batch):
            if not isinstance(item, dict) or not item.get("prompt"):
                return None, f"batch[{i}] must be an object with a non-empty 'prompt' field"
            item_role = item.get("role") or tool_input.get("role")
            # finding 22: an unknown role in a batch item (or the shared
            # top-level one) used to silently resolve to the session model.
            role_error = _invalid_role_error(item_role)
            if role_error is not None:
                return None, f"batch[{i}]: {role_error}"
            jobs.append({
                "prompt": item["prompt"], "role": item_role,
                "model": item.get("model") or tool_input.get("model"),
                # 2.0.2 review finding 21: a batch item used to drop the
                # top-level `effort` entirely when it had none of its own,
                # although docs/SUBAGENTS.md says it falls back.
                "effort": item.get("effort") or tool_input.get("effort"),
                "description": (item.get("description") or description)[:60],
            })
        return jobs, None
    try:
        n = int(tool_input.get("count"))
    except (TypeError, ValueError):
        return None, "count must be a positive integer"
    if n <= 0:
        return None, "count must be a positive integer"
    if n > 50:
        return None, "count may not exceed 50 in one call"
    prompt = tool_input.get("prompt") or spec.initial_prompt or description
    if not prompt:
        return None, "The prompt parameter is required"
    role_error = _invalid_role_error(tool_input.get("role"))  # finding 22
    if role_error is not None:
        return None, role_error
    jobs = [{"prompt": prompt, "role": tool_input.get("role"), "model": tool_input.get("model"),
             # finding 21: `count` used to ignore the call's own `effort`
             # outright -- every job got `None` regardless of what was asked.
             "effort": tool_input.get("effort"), "description": description[:60]} for _ in range(n)]
    return jobs, None


def _run_agent_fanout(*, runtime: AgentRuntime, spec: AgentSpec, tool_id: str, tool_input: dict,
                       on_event=None) -> "tuple[list, object]":
    """Halo 2.0.2 round 3 (brief C): `count`/`batch` -- spawns several
    children of `spec` and waits for ALL of them, capped at `effective_
    max_concurrent(runtime)` the same way several separate Agent tool_use
    blocks in one turn already are (`agent/loop.py`'s own `_run_agent_
    batch`). Excess jobs QUEUE: every job's own agent_id/task_id is
    minted and a `subagent_queued` event fired for it, in SPAWN order,
    BEFORE any pool worker has even started picking them up -- so the
    tasks panel shows every job immediately, never only once a slot
    frees. Returns ONE combined ToolResult: one `<task_result>` section
    per child, in SPAWN order (never completion order -- a slow 2nd
    child must not reshuffle a fast 5th one ahead of it in the model's
    own reading of the result). `on_event`, same contract as `run_agent_
    call`'s own: None means no genuinely LIVE consumer exists, so a
    child's own live permission/question asks are disabled (same
    `_subagent_live_asks` guard the single-spawn path already applies)
    rather than risking a child parked forever waiting for an answer
    nothing will ever deliver."""
    from halo_harness.tools.base import ToolResult

    jobs, error = _prepare_fanout_jobs(spec, tool_input)
    if error is not None:
        return [], ToolResult(error, is_error=True)
    n = len(jobs)
    if n > 1:
        for i, job in enumerate(jobs):
            job["description"] = f"{job['description']} ({i + 1}/{n})"

    parent = runtime.parent
    live_asks_ok = on_event is not None
    all_events: list = []
    events_lock = threading.Lock()

    def _emit(ev) -> None:
        with events_lock:
            all_events.append(ev)
        if on_event is not None:
            on_event(ev)

    prepared = []  # [(agent_id, task_id, job), ...], in spawn order
    for job in jobs:
        agent_id = _new_agent_id()
        new_task_id = uuid.uuid4().hex[:12]
        with runtime.lock:
            runtime.tasks[new_task_id] = {"child_session_id": f"agent-{agent_id}", "spec_name": spec.name,
                                           "cwd": str(parent.cwd), "abort_event": None,
                                           "running_in_process": True, "status": "queued"}
        # A minimal meta.json stub, written up front -- the tasks panel
        # reads meta.json (what survives a `-c` resume), not just this
        # in-memory dict, so it must see "queued" right away; `_build_
        # child_session` (inside `_run_one_fanout_child`) naturally
        # overwrites/merges this SAME file with "running" once this job
        # actually starts (`_write_meta` merges onto existing content).
        _, meta_path = _child_log_paths(parent, agent_id)
        _write_meta(meta_path, {"agent_id": agent_id, "type": spec.name, "description": job["description"],
                                 "status": "queued", "task_id": new_task_id, "parent_tool_use_id": tool_id})
        queued_ev = events.Event("subagent_queued", {"agent_id": agent_id, "name": spec.dock_label or spec.name,
                                                        "description": job["description"],
                                                        "parent_tool_use_id": tool_id, "task_id": new_task_id})
        queued_ev.agent_id = agent_id
        _emit(queued_ev)
        prepared.append((agent_id, new_task_id, job))

    results: list = [None] * n

    def _run_one(index: int) -> None:
        agent_id, new_task_id, job = prepared[index]
        try:
            results[index] = _run_one_fanout_child(
                runtime=runtime, spec=spec, agent_id=agent_id, new_task_id=new_task_id, job=job,
                tool_id=tool_id, on_event=_emit, live_asks_ok=live_asks_ok,
            )
        except Exception as e:  # this module's own contract: never raise out of a spawn path
            log.exception("fan-out child dispatch failed for task_id=%r", new_task_id)
            results[index] = (f"Sub-agent dispatch failed: {type(e).__name__}: {e}", True)

    max_workers = min(effective_max_concurrent(runtime), n)
    # 2.0.2 review finding 10: `wrap_for_pool`, called on THIS (the
    # submitting) thread -- see `SessionConcurrencyGate`'s own docstring
    # for why a bare `threading.local` cannot do this across the thread
    # boundary a fresh pool worker always is.
    with runtime.concurrency_semaphore.pool(max_workers=max_workers, limit=effective_max_concurrent(runtime)) as pool:
        for f in [pool.submit(_run_one, i) for i in range(n)]:
            f.result()

    sections = []
    any_error = False
    for i, ((_agent_id, new_task_id, _job), (text, is_error)) in enumerate(zip(prepared, results, strict=True)):
        any_error = any_error or is_error
        sections.append(f"--- child {i + 1}/{n} (task_id={new_task_id}) ---\n{text}")
    # 2.0.2 review finding 27: each section is individually uncapped
    # (`AgentTool.result_cap=None` -- "this tool manages its own
    # truncation", unlike every other tool, which `spill_and_truncate`
    # in agent/loop.py caps generically) -- `count=50` could return
    # ~1.5M chars in one tool_result. Capped the same way the single-
    # spawn/resume paths already cap THEIR own result (`RESULT_CAP`,
    # head/tail + a spill file for the full combined text).
    from halo_harness.tools.truncate import spill_and_truncate
    combined = spill_and_truncate("\n\n".join(sections), cap=RESULT_CAP,
                                   session_dir=parent.log.dir / parent.log.session_id, tool_use_id=tool_id)
    return all_events, ToolResult(combined, is_error=any_error)


def _run_one_fanout_child(*, runtime: AgentRuntime, spec: AgentSpec, agent_id: str, new_task_id: str,
                           job: dict, tool_id: str, on_event, live_asks_ok: bool) -> "tuple[str, bool]":
    """One `count`/`batch` job's own full build-run-finalize sequence --
    the same steps `run_agent_call`'s single-spawn foreground path runs,
    parameterized by a PRE-MINTED `agent_id`/`task_id` (minted by `_run_
    agent_fanout` before this job ever reached a pool slot, so it could
    be announced as "queued" immediately) and this job's own prompt/
    role/model/effort. `on_event` here is always a real callable (`_run_
    agent_fanout`'s own `_emit`, which both collects and optionally
    forwards); `live_asks_ok` carries whether the ORIGINAL caller gave
    `_run_agent_fanout` a genuinely live channel -- a bare `_emit` with
    nothing live downstream must still disable `_subagent_live_asks`,
    exactly like the single-spawn path's own `on_event is None` check."""
    from halo_harness.tools.truncate import spill_and_truncate

    parent = runtime.parent
    role_name = job["role"] or spec.role
    try:
        # 2.0.2 review finding 11 (major): after Esc, every QUEUED fan-out
        # job used to still build a child session (a worktree included,
        # when isolated), fire SubagentStart/TaskCreated + SubagentStop/
        # TaskCompleted, and write logs -- undoing H5c finding 7's own
        # abort-batch contract, which `_run_agent_batch` (several separate
        # Agent tool_use blocks in one turn) already enforces. A job
        # already past this point shares `runtime.parent.abort` with every
        # OTHER foreground child (`_build_child_session`'s own `abort=`),
        # so it already reacts to Esc mid-turn with no change needed here.
        if parent.abort.is_set():
            _, not_started_meta_path = _child_log_paths(parent, agent_id)
            _write_meta(not_started_meta_path, {"status": "not_started", "is_error": True, "finished": time.time()})
            return "Sub-agent not started: interrupted by the user.", True
        # 2.0.2 review finding 5 (major): the SAME hard-stop-before-spawn
        # check the single-spawn path already makes -- a fan-out job used
        # to skip org budgets entirely (checked once, at the top of the
        # whole count/batch call, never again as later jobs in the SAME
        # batch keep spending).
        if runtime.org_budget is not None:
            refusal = runtime.org_budget.refusal_before_spawn(spec.name, parent.cost_meter.total_usd)
            if refusal:
                _, refused_meta_path = _child_log_paths(parent, agent_id)
                _write_meta(refused_meta_path, {"status": "not_started", "is_error": True, "finished": time.time()})
                return refusal, True
        child, meta_path = _build_child_session(
            runtime=runtime, spec=spec, agent_id=agent_id, model_override=job["model"],
            parent_tool_use_id=tool_id, background=False, role_override=job["role"],
            effort_override=job["effort"],
        )
        if not live_asks_ok:
            child._subagent_live_asks = False
        since_index = len(child.log.nodes())
        runtime.live_children[agent_id] = child
        _write_meta(meta_path, {"task_id": new_task_id, "background": False})
        with runtime.lock:
            entry = runtime.tasks.get(new_task_id)
            if entry is not None:
                entry["status"] = "running"

        _dock_name = spec.dock_label or spec.name
        start_ev = events.Event("subagent_start", {"agent_id": agent_id, "name": _dock_name,
                                                     "description": job["description"],
                                                     "parent_tool_use_id": tool_id, "task_id": new_task_id})
        start_ev.agent_id = agent_id
        _fire_subagent_hook(child, "SubagentStart")
        _fire_task_hook(parent, "TaskCreated", task_id=new_task_id, spec_name=spec.name,
                         description=job["description"])
        on_event(start_ev)

        try:
            # 2.0.2 review finding 10: the session-/org-wide cap, not just
            # this one fan-out call's own pool (sized separately, below).
            with runtime.concurrency_semaphore:
                child_events = _run_child_to_completion(child, job["prompt"], agent_id=agent_id,
                                                          parent_tool_use_id=tool_id, on_event=on_event)
        finally:
            child.close_cc()
            runtime.live_children.pop(agent_id, None)

        text = _final_text_from_log(child)
        is_error, abnormal_reason = _child_turn_outcome(child_events)
        _fire_subagent_hook(child, "SubagentStop")
        _fire_task_hook(parent, "TaskCompleted", task_id=new_task_id, spec_name=spec.name,
                         description=job["description"])
        _write_meta(meta_path, {"status": "completed", "is_error": is_error, "finished": time.time()})
        _rollup_child_cost_into_parent(parent, child, agent_id=agent_id, since_index=since_index, role=role_name)
        # 2.0.2 review finding 5: a fan-out job's own spend now reaches the
        # SAME budget tracker a single spawn already does -- this path
        # never called `record_spend` at all before. `child.cost_meter.
        # total_usd` is this job's own delta already (a fresh CostMeter
        # per Session construction), never a before/after subtraction on
        # the shared PARENT meter, which concurrent sibling jobs finishing
        # at the same moment would double-count.
        budget_note = (runtime.org_budget.record_spend(spec.name, child.cost_meter.total_usd,
                                                          parent.cost_meter.total_usd)
                       if runtime.org_budget is not None else None)
        with parent._agent_notices_lock:
            parent.permission_denials.extend(child.permission_denials)
        end_ev = events.Event("subagent_end", {"agent_id": agent_id, "name": _dock_name,
                                                  "parent_tool_use_id": tool_id, "task_id": new_task_id,
                                                  "is_error": is_error})
        end_ev.agent_id = agent_id
        on_event(end_ev)
        if is_error:
            text = (f"[sub-agent did not finish normally ({abnormal_reason}) -- this may be a "
                     f"stale/partial answer]\n{text}")
        worktree_note = _finalize_child_isolation_worktree(child)
        if worktree_note:
            text = f"{text}\n\n{worktree_note}"
        if budget_note:
            text = f"{text}\n\n{budget_note}"
        wrapped = _wrap_task_result(text, new_task_id)
        capped = spill_and_truncate(wrapped, cap=RESULT_CAP, session_dir=parent.log.dir / parent.log.session_id,
                                     tool_use_id=f"{tool_id}-{agent_id}")
        return capped, is_error
    finally:
        with runtime.lock:
            entry = runtime.tasks.get(new_task_id)
            if entry is not None:
                entry["running_in_process"] = False


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
    # Halo 2.0.2 round 2 (brief B): "depth comes from the tree" -- an org
    # run's own `max_depth` (`org_tree_depth(org) + 1`, set by `run_org_
    # call` and propagated to every descendant by `_build_child_session`)
    # overrides the module-wide `MAX_DEPTH` when set; `None` for every
    # non-org caller, so this is byte-for-byte the old check otherwise.
    # Round 3 (brief C): that "otherwise" now reads the `agents.max_depth`
    # config knob instead of the bare module constant -- see `effective_
    # max_depth`'s own docstring.
    _max_depth = effective_max_depth(runtime)
    if runtime.depth >= _max_depth:
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
    # Halo 2.0.2 round 7 (brief 3b): an org (or per-position) budget
    # already tripped refuses the spawn outright, BEFORE any model call
    # happens -- same shape as the depth check above. `org_budget` is
    # `None` for every non-org caller, so this is a no-op everywhere else.
    if runtime.org_budget is not None:
        refusal = runtime.org_budget.refusal_before_spawn(subagent_type, parent.cost_meter.total_usd)
        if refusal:
            return [], ToolResult(refusal, is_error=True)

    # Halo 2.0.2 round 3 (brief C): `count` (N identical prompts) / `batch`
    # (a list of per-child prompt+role+model+effort overrides) spawn
    # several children of `subagent_type` in ONE call, capped at `agents.
    # max_concurrent` the same way several separate Agent tool_use blocks
    # in one turn already are -- see `_run_agent_fanout`'s own docstring.
    if tool_input.get("count") is not None or tool_input.get("batch") is not None:
        if bool(tool_input.get("run_in_background")) or spec.background:
            return [], ToolResult("count/batch cannot be combined with run_in_background -- the parent "
                                   "already waits on every fan-out child; run them one at a time in the "
                                   "background instead if that's what you need.", is_error=True)
        return _run_agent_fanout(runtime=runtime, spec=spec, tool_id=tool_id, tool_input=tool_input,
                                  on_event=on_event)

    description = tool_input.get("description") or spec.description[:60]
    prompt = tool_input.get("prompt") or spec.initial_prompt or description
    background = bool(tool_input.get("run_in_background")) or spec.background
    model_override = tool_input.get("model")
    # V2c (H15): `Agent(role=...)` overrides this agent's own file/built-in
    # role for just this one call; `role_name` (used below for the cost
    # rollup's own `stats --roles` tag) mirrors exactly what `resolve_agent_
    # model` inside `_build_child_session` resolves the role AGAINST.
    role_override = tool_input.get("role")
    # finding 22: a typo'd/unknown `Agent(role=...)` used to silently
    # resolve against the session model instead of erroring the way an
    # unknown subagent_type already does.
    role_error = _invalid_role_error(role_override)
    if role_error is not None:
        return [], ToolResult(role_error, is_error=True)
    role_name = role_override or spec.role
    # Halo 2.0.2 round 3 (brief C): `Agent(effort=...)` overrides this
    # agent's own file/role/parent-inherited effort for just this one
    # call -- same rung `_run_one_fanout_child`'s own per-job `effort`
    # already occupies (see `_build_child_session`'s own `_effective_
    # effort` precedence chain).
    effort_override = tool_input.get("effort")

    agent_id = _new_agent_id()
    try:
        child, meta_path = _build_child_session(
            runtime=runtime, spec=spec, agent_id=agent_id, model_override=model_override, parent_tool_use_id=tool_id,
            background=background, role_override=role_override, effort_override=effort_override,
        )
    except Exception as e:
        # 2.0.2 review finding 4 (major): this module's own docstring says
        # "Never raises" -- an unresolvable model (a bad org position
        # `model:`, or a `/role` pointed at a bogus ref) raised
        # InvalidModelError straight out of here instead. The TUI's own
        # `/org run` worker had no guard for that at all (took the whole
        # app down with it, see `tui/slash.py::_run_org_worker`'s own
        # fix). A minimal meta.json is written here too, so the tasks
        # panel shows this attempt as a real, finished (errored) entry
        # instead of nothing at all.
        _, failed_meta_path = _child_log_paths(parent, agent_id)
        _write_meta(failed_meta_path, {"agent_id": agent_id, "type": spec.name, "description": description,
                                        "status": "completed", "is_error": True, "finished": time.time(),
                                        "parent_tool_use_id": tool_id})
        return [], ToolResult(f"Could not start sub-agent '{spec.name}': {type(e).__name__}: {e}", is_error=True)
    # finding 10 (W6a): `_build_child_session` sets `_subagent_live_asks`
    # from `parent.interactive` ALONE, with no way to know whether THIS
    # caller actually gave it a live channel to forward an ask through --
    # a `context: fork`/`agent` Skill (tools/skill.py) calls this directly
    # with no `on_event` at all, so a foreground child of an interactive
    # parent got the live/blocking ask path (`_await_permission_decision`/
    # `_await_reply`) with its `permission_request`/`question` event
    # reaching no live consumer whatsoever -- the turn hung until Esc,
    # nothing ever visibly asked. A BACKGROUND child is unaffected: it has
    # its OWN separate live-ask channel (`_bg_run`'s own `on_event`, built
    # below from `parent._event_sink`), unrelated to this `on_event`
    # parameter.
    if not background and on_event is None:
        child._subagent_live_asks = False
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

    # Halo 2.0.2 round 2 (brief B): an org position's own `dock_label`
    # ("<title> (<role>)") in place of the bare name on the dock -- `None`
    # for every non-org spec, so this is just `spec.name` as before.
    _dock_name = spec.dock_label or spec.name
    start_ev = events.Event("subagent_start", {"agent_id": agent_id, "name": _dock_name, "description": description,
                                                 "parent_tool_use_id": tool_id, "task_id": new_task_id})
    start_ev.agent_id = agent_id
    _fire_subagent_hook(child, "SubagentStart")
    _fire_task_hook(parent, "TaskCreated", task_id=new_task_id, spec_name=spec.name, description=description)
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
                # W4a: forwards ONLY this BACKGROUND child's live ASKS
                # (permission/question/plan) straight into the parent's own
                # live event queue (`Session._event_sink`, set by `run()` --
                # the TUI's worker-thread command pump; None for a `-p`/
                # bare-Session run, where this is exactly the old no-
                # forwarding behaviour) -- the SAME dock a foreground
                # child's asks already reach. Deliberately narrower than a
                # foreground child's own full `on_event` forwarding: the
                # brief asks for "live permission asks", not making a
                # background task's whole output stream suddenly live (it
                # still reports via the existing completion notice).
                bg_sink = getattr(parent, "_event_sink", None)
                on_event = ((lambda ev: bg_sink(ev) if ev.kind in ("permission_request", "question", "plan_review")
                             else None) if bg_sink is not None else None)
                with runtime.concurrency_semaphore:  # 2.0.2 review finding 10: session-wide, not just one pool's own
                    child_events = _run_child_to_completion(child, prompt, agent_id=agent_id,
                                                             parent_tool_use_id=tool_id, on_event=on_event)
                text = _final_text_from_log(child)
                _fire_subagent_hook(child, "SubagentStop")
                _fire_task_hook(parent, "TaskCompleted", task_id=new_task_id, spec_name=spec.name, description=description)
                _write_meta(meta_path, {"status": "completed"})
                # H9 whole-tree review finding 13: roll this background child's
                # own usage/cost into the parent BEFORE the completion notice
                # is queued, so by the time the parent's next turn (which
                # applies that notice) actually runs, `--max-budget-usd`/
                # `/stats` already reflect it.
                _rollup_child_cost_into_parent(parent, child, agent_id=agent_id, since_index=since_index, role=role_name)
                # 2.0.2 review finding 5 (major): `child.cost_meter.total_usd`
                # is already scoped to exactly this call (a fresh CostMeter
                # per Session construction) -- a before/after subtraction on
                # the shared PARENT meter double-counted whenever another
                # sibling's own rollup landed on that SAME shared total in
                # between the two reads (concurrent fan-out jobs, or two
                # backgrounded sub-agents finishing at once).
                budget_note = (runtime.org_budget.record_spend(
                    spec.name, child.cost_meter.total_usd, parent.cost_meter.total_usd)
                    if runtime.org_budget is not None else None)
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
                # Halo 2.0.2 round 3 (brief C): the tasks panel's own
                # "finished agents stay listed ... with their final
                # status" reads THIS field (meta.json has no other record
                # of is_error -- `_write_meta` above fired before this was
                # known) plus a stamped finish time, so a closed panel
                # reopened later still shows the real outcome, not just
                # "completed" for both a clean and a failed run alike.
                _write_meta(meta_path, {"is_error": is_error, "finished": time.time()})
                # review finding 20: the foreground path always emits
                # `subagent_end` (see the mirror-image `end_ev` below the
                # foreground `try/finally`); `_bg_run` never did, so a live
                # TUI's SubAgentCard (mounted on `subagent_start`, which DOES
                # reach the dock via `bg_sink`) never got `card.finish()`
                # and kept ticking "thinking Ns" forever, even after the
                # completion notice landed right below. Reuses the same
                # `bg_sink` the live-ask forwarder above already resolved --
                # None for a `-p`/bare-Session run, exactly like start_ev's
                # own live forwarding, so headless callers see no behavior
                # change at all.
                end_ev = events.Event("subagent_end", {"agent_id": agent_id, "name": _dock_name,
                                                          "parent_tool_use_id": tool_id,
                                                          "task_id": new_task_id, "is_error": is_error})
                end_ev.agent_id = agent_id
                if bg_sink is not None:
                    bg_sink(end_ev)
                if abnormal_reason and abnormal_reason not in ("interrupted",):
                    status_bits.append(abnormal_reason)
                status_suffix = f" [{'; '.join(status_bits)}]" if status_bits else ""
                if is_error:
                    text = f"(this sub-agent may not have finished normally)\n{text}"
                worktree_note = _finalize_child_isolation_worktree(child)
                if worktree_note:
                    text = f"{text}\n\n{worktree_note}"
                if budget_note:
                    text = f"{text}\n\n{budget_note}"
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
        with runtime.concurrency_semaphore:  # 2.0.2 review finding 10: session-wide, not just one pool's own
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
    _fire_task_hook(parent, "TaskCompleted", task_id=new_task_id, spec_name=spec.name, description=description)
    # Halo 2.0.2 round 3 (brief C): `is_error`/`finished` -- see _bg_run's
    # own matching comment just above.
    _write_meta(meta_path, {"status": "completed", "is_error": is_error, "finished": time.time()})
    # H9 whole-tree review finding 13: see the background path's own
    # comment above -- a FOREGROUND child's usage/cost gets the same
    # rollup, just synchronously here instead of at the end of `_bg_run`.
    _rollup_child_cost_into_parent(parent, child, agent_id=agent_id, since_index=since_index, role=role_name)
    # 2.0.2 review finding 5: `child.cost_meter.total_usd` is this call's
    # own delta already -- see `_bg_run`'s own matching comment above for
    # why a before/after subtraction on the shared PARENT meter is wrong.
    budget_note = (runtime.org_budget.record_spend(spec.name, child.cost_meter.total_usd,
                                                     parent.cost_meter.total_usd)
                   if runtime.org_budget is not None else None)
    # H6 known v1 gap (D10) / B must-do: a non-interactive child's own
    # "ask" denials (agent/loop.py's `_resolve_tool_call`) land in the
    # CHILD's own `permission_denials` list, which nothing outside this
    # function would otherwise ever read -- merged into the PARENT's here
    # so print mode's top-level result (headless.py reads `session.
    # permission_denials`, the TOP session only) actually surfaces a
    # sub-agent's denied tool calls instead of silently losing them.
    parent.permission_denials.extend(child.permission_denials)
    end_ev = events.Event("subagent_end", {"agent_id": agent_id, "name": _dock_name, "parent_tool_use_id": tool_id,
                                             "task_id": new_task_id, "is_error": is_error})
    end_ev.agent_id = agent_id
    if on_event is not None:
        on_event(end_ev)  # H5c finding 8: live too, same reasoning as start_ev above

    if is_error:
        text = f"[sub-agent did not finish normally ({abnormal_reason}) -- this may be a stale/partial answer]\n{text}"
    worktree_note = _finalize_child_isolation_worktree(child)
    if worktree_note:
        text = f"{text}\n\n{worktree_note}"
    if budget_note:
        text = f"{text}\n\n{budget_note}"
    wrapped = _wrap_task_result(text, new_task_id)
    capped = spill_and_truncate(wrapped, cap=RESULT_CAP, session_dir=parent.log.dir / parent.log.session_id,
                                 tool_use_id=tool_id)
    return [start_ev, *child_events, end_ev], ToolResult(capped, is_error=is_error)


def run_org_call(*, runtime: AgentRuntime, tool_id: str, tool_input: dict, tool_name: str,
                  on_event=None) -> "tuple[list, object]":
    """`Agent(org=<name>, prompt=<goal>)` / `/org run <name> "<goal>"`
    (Halo 2.0.2 round 2, brief B): spawns the org's ROOT position as an
    ORDINARY child of `runtime.parent`, through `run_agent_call` itself
    ("results flow back up as ordinary sub-agent results"), against a
    FRESH `AgentRuntime` that swaps in the org's own positions (one
    `AgentSpec` per title, each with its own `reports` as its `delegate_
    restriction` -- `orgs.position_agent_specs`) for `runtime.agents`,
    and sets `max_depth` from the org's own tree shape, RELATIVE to the
    caller's own depth ("depth comes from the tree": the caller's own
    depth, plus one hop for THIS call, plus the longest root-to-leaf path
    `orgs.org_tree_depth` finds in the org's own `reports` graph -- a
    caller already at ITS OWN depth cap is refused outright, same as an
    ordinary Agent call) and `max_concurrent` from the org's own value
    (or `None`, falling
    through to the `agents.max_concurrent` config knob via `effective_
    max_concurrent`). Shares the CALLER's own `tasks`/`lock` so task_id
    resume/`TaskStop` bookkeeping for whatever this spawns stays part of
    the SAME session-wide map. Never raises, matching `run_agent_call`'s
    own contract -- every failure becomes an `is_error` ToolResult."""
    from halo_harness.orgs import list_orgs, load_org, org_tree_depth, position_agent_specs, root_position, \
        validate_org
    from halo_harness.tools.base import ToolResult

    # 2.0.2 review finding 9 (major) part a: the SAME depth-cap check
    # `run_agent_call` makes for an ordinary Agent call -- without it, a
    # sub-agent already at ITS OWN depth cap could still START a whole
    # org tree (max_depth below is only ever checked AFTER being freshly
    # OVERWRITTEN for the org run, so the caller's own pre-existing cap
    # never got a say), bypassing "Sub-agents cannot spawn further
    # sub-agents" entirely.
    _caller_max_depth = effective_max_depth(runtime)
    if runtime.depth >= _caller_max_depth:
        return [], ToolResult(
            "Sub-agents cannot spawn further sub-agents (depth limit reached) -- finish this task "
            "yourself instead of delegating further.", is_error=True,
        )

    tool_input = tool_input if isinstance(tool_input, dict) else {}
    name = tool_input.get("org")
    if not name:
        return [], ToolResult("The org parameter is required", is_error=True)
    goal = tool_input.get("prompt") or ""
    if not goal:
        return [], ToolResult("The prompt parameter is required", is_error=True)
    state_dir = runtime.parent.state_dir
    org = load_org(name, state_dir=state_dir)
    if org is None:
        known = ", ".join(list_orgs(state_dir=state_dir)) or "(none)"
        return [], ToolResult(f"Unknown organization {name!r} (or it failed validation). "
                               f"Known organizations: {known}", is_error=True)
    problems = validate_org(org)
    if problems:
        return [], ToolResult(f"Organization {name!r} is invalid: {'; '.join(problems)}", is_error=True)
    root = root_position(org)
    if root is None:
        return [], ToolResult(f"Organization {name!r} has no single root.", is_error=True)
    # Halo 2.0.2 round 7 (brief 3b, "goals"): this run's own goal becomes
    # the root task on the shared board -- best-effort (a missing/
    # unwritable session_dir, e.g. a bare unit-test Session, just means no
    # goal task exists; every position still runs normally either way).
    goal_task_id = None
    session_dir = runtime.parent.log.dir / runtime.parent.log.session_id  # same path ctx.session_dir uses (agent/loop.py)
    try:
        from halo_harness.tools.task_board import create_task
        goal_task_id = create_task(session_dir, title=f"Goal: {goal}", notes=f"organization={name}", kind="goal")
    except Exception:
        goal_task_id = None
    # Halo 2.0.2 round 7 (brief 3b, "budgets"): a tracker shared by every
    # position in this run (and every descendant's own nested
    # AgentRuntime, via `_build_child_session`'s `org_budget=` copy) --
    # `baseline_usd` is THIS call's own starting point, so a budget never
    # counts spend from whatever the parent session already did before
    # this org run started.
    from halo_harness.orgs import build_budget_tracker
    org_budget = build_budget_tracker(org, baseline_usd=runtime.parent.cost_meter.total_usd)
    org_runtime = AgentRuntime(
        parent=runtime.parent, agents=position_agent_specs(org, goal_task_id=goal_task_id), routes=runtime.routes,
        role_table=runtime.role_table, cli_role_overrides=runtime.cli_role_overrides, depth=runtime.depth,
        tasks=runtime.tasks, lock=runtime.lock,
        # 2.0.2 review finding 29: shared with the CALLER's own runtime
        # (never the usual "fresh dict per runtime" -- `org_runtime` is a
        # throwaway wrapper for spawning the org's root position through
        # the ordinary `run_agent_call` path, not a real nested child
        # session) so the tasks panel's own `_walk_live_children(session.
        # agent_runtime)` can actually see the org's root position (and,
        # transitively, every descendant) while it runs, and `cc_runtime.
        # close_cc()` on quit reaches any `cc:` position inside it too.
        live_children=runtime.live_children,
        # 2.0.2 review finding 9 part b: RELATIVE to the caller's own
        # depth, never absolute -- `org_tree_depth(org) + 1` alone (the
        # old code) ignored `runtime.depth` entirely, so an org started
        # from an already-nested caller (depth 1) got the SAME cap as one
        # started fresh (depth 0), silently losing however many levels
        # the caller was already down. Verified: an `AgentRuntime` at
        # depth 1 running `org="company"` (tree depth 3) used to build
        # `{'depth': 1, 'max_depth': 4}` -- only 3 more hops below depth
        # 1, when the whole tree needs 1 + 3 + 1 = 5.
        max_depth=runtime.depth + org_tree_depth(org) + 1,
        max_concurrent=org.get("max_concurrent"), org_budget=org_budget,
    )
    # 2.0.2 review finding 10 (major): a FRESH semaphore, sized for THIS
    # org run specifically (its own `max_concurrent`, or the config
    # default via `effective_max_concurrent`) -- shared down the WHOLE
    # org tree by `_build_child_session`'s own propagation, so "an org's
    # own max_concurrent" finally caps the WHOLE run, not just whichever
    # one position's own fan-out pool happened to read it.
    org_runtime.concurrency_semaphore = SessionConcurrencyGate(effective_max_concurrent(org_runtime))
    inner_input = {"subagent_type": root["title"], "prompt": goal,
                   "description": tool_input.get("description") or f"Run org {name}"}
    # Fix-pass notes ("outer org task resume"): tag whatever task_id this
    # call just minted for the root position with enough to rebuild an
    # equivalent org_runtime later -- see `_resume_task`'s own matching
    # comment. Diffed by key (never assumed to be exactly one new entry)
    # since a root position itself using count/batch would mint more than
    # one; every new entry this one `run_agent_call` call produced is the
    # SAME org run, so all of them get the same tag.
    before_task_ids = set(runtime.tasks)
    result = run_agent_call(runtime=org_runtime, tool_id=tool_id, tool_input=inner_input, tool_name=tool_name,
                             on_event=on_event)
    for new_id in set(org_runtime.tasks) - before_task_ids:
        entry = org_runtime.tasks.get(new_id)
        if isinstance(entry, dict):
            entry["org_name"] = name
            entry["org_max_depth"] = org_runtime.max_depth
            entry["org_max_concurrent"] = org_runtime.max_concurrent
            entry["org_budget"] = org_budget
            entry["goal_task_id"] = goal_task_id
    return result


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
    effective_runtime = runtime
    # Fix-pass notes ("outer org task resume" -- REFUTED as "loses max_
    # depth/max_concurrent", it was worse: resume failed outright). The
    # org's ROOT position (and every other position spawned through
    # `run_org_call`) is never a name in the CALLER's own `runtime.agents`
    # catalog -- that mapping only ever existed on the ephemeral `org_
    # runtime` `run_org_call` built and discarded once its own call
    # returned, so `spec` above is always None for one of these.
    # `run_org_call` now tags the task record with enough to rebuild an
    # equivalent runtime here: the org's own name/depth/concurrency/
    # budget, so a resumed org position runs inside the SAME caps (and
    # can still delegate to its own reports) the original run had.
    if spec is None and record.get("org_name"):
        from halo_harness.orgs import load_org, position_agent_specs
        org = load_org(record["org_name"], state_dir=parent.state_dir)
        if org is not None:
            agents = position_agent_specs(org, goal_task_id=record.get("goal_task_id"))
            spec = agents.get(record["spec_name"])
            if spec is not None:
                effective_runtime = AgentRuntime(
                    parent=runtime.parent, agents=agents, routes=runtime.routes,
                    role_table=runtime.role_table, cli_role_overrides=runtime.cli_role_overrides,
                    depth=runtime.depth, tasks=runtime.tasks, lock=runtime.lock,
                    max_depth=record.get("org_max_depth"), max_concurrent=record.get("org_max_concurrent"),
                    org_budget=record.get("org_budget"), concurrency_semaphore=runtime.concurrency_semaphore,
                )
    if spec is None:
        return [], ToolResult(f"The agent type for task_id {task_id!r} is no longer available.", is_error=True)

    agent_id = record["child_session_id"][len("agent-"):] if record["child_session_id"].startswith("agent-") \
        else record["child_session_id"]
    # V2c (H15): a resume may itself carry a fresh `role=` override; else
    # this resumed call keeps resolving against the SAME agent's own role.
    role_override = tool_input.get("role")
    # finding 22: same check as the single-spawn path above -- a resume's
    # own fresh `role=` override was never validated either.
    role_error = _invalid_role_error(role_override)
    if role_error is not None:
        return [], ToolResult(role_error, is_error=True)
    role_name = role_override or spec.role
    # Finding 5: the SAME hard-stop-before-spawn check run_agent_call's
    # single-spawn path already makes -- a resume used to skip org
    # budgets entirely.
    if effective_runtime.org_budget is not None:
        refusal = effective_runtime.org_budget.refusal_before_spawn(spec.name, parent.cost_meter.total_usd)
        if refusal:
            return [], ToolResult(refusal, is_error=True)
    try:
        child, meta_path = _build_child_session(
            runtime=effective_runtime, spec=spec, agent_id=agent_id, model_override=tool_input.get("model"),
            parent_tool_use_id=tool_id, background=False,  # a resume always runs in the foreground
            role_override=role_override, effort_override=tool_input.get("effort"),
        )
    except Exception as e:
        # Finding 4: same "never raises" contract run_agent_call's own
        # single-spawn path now guards with -- an unresolvable model (a
        # bad `model=` override on the resume call, or a position whose
        # own `model:` only went bad after this task was created) used to
        # raise straight out of here.
        _, failed_meta_path = _child_log_paths(parent, agent_id)
        _write_meta(failed_meta_path, {"status": "completed", "is_error": True, "finished": time.time()})
        return [], ToolResult(f"Could not resume sub-agent for task_id {task_id!r}: {type(e).__name__}: {e}",
                               is_error=True)
    # H9 whole-tree review finding 13: see run_agent_call's own comment on
    # its identically-named local -- a resume's own child log already
    # carries every PRIOR call's usage nodes; only what's appended from
    # here on is THIS call's own new spend.
    since_index = len(child.log.nodes())
    runtime.live_children[agent_id] = child  # H11b finding 14, see run_agent_call's own comment
    prompt = tool_input.get("prompt") or "Please continue."
    try:
        # finding 10: the session-/org-wide cap, never just one pool's own.
        with effective_runtime.concurrency_semaphore:
            child_events = _run_child_to_completion(child, prompt, agent_id=agent_id, parent_tool_use_id=tool_id,
                                                     on_event=on_event)
    finally:
        child.close_cc()
        runtime.live_children.pop(agent_id, None)
    text = _final_text_from_log(child)
    # H9 whole-tree review finding 26: same treatment as run_agent_call's
    # own foreground path -- see its comment.
    is_error, abnormal_reason = _child_turn_outcome(child_events)
    _write_meta(meta_path, {"status": "completed", "is_error": is_error, "finished": time.time()})
    _rollup_child_cost_into_parent(parent, child, agent_id=agent_id, since_index=since_index, role=role_name)
    # Finding 5: a resumed call's own spend now reaches the SAME budget
    # tracker a fresh spawn already does -- `child.cost_meter.total_usd`
    # (never a before/after subtraction on the shared PARENT meter) is
    # "this call's own delta", see run_agent_call's own matching comment.
    budget_note = (effective_runtime.org_budget.record_spend(spec.name, child.cost_meter.total_usd,
                                                                parent.cost_meter.total_usd)
                   if effective_runtime.org_budget is not None else None)
    parent.permission_denials.extend(child.permission_denials)  # H6/D10, see run_agent_call's own comment
    if is_error:
        text = f"[sub-agent did not finish normally ({abnormal_reason}) -- this may be a stale/partial answer]\n{text}"
    worktree_note = _finalize_child_isolation_worktree(child)
    if worktree_note:
        text = f"{text}\n\n{worktree_note}"
    if budget_note:
        text = f"{text}\n\n{budget_note}"
    wrapped = _wrap_task_result(text, task_id)
    capped = spill_and_truncate(wrapped, cap=RESULT_CAP, session_dir=parent.log.dir / parent.log.session_id,
                                 tool_use_id=tool_id)
    return child_events, ToolResult(capped, is_error=is_error)


def _walk_live_children(runtime: "AgentRuntime"):
    """Yields `(agent_id, child_session)` for every CURRENTLY live
    descendant reachable from `runtime`, at any depth -- each child's
    own `agent_runtime.live_children` is a FRESH dict (never shared by
    reference across the tree, unlike `tasks`/`lock`), so a grandchild's
    liveness is only visible by recursing into its own parent's dict."""
    for agent_id, child in list(runtime.live_children.items()):
        yield agent_id, child
        child_runtime = getattr(child, "agent_runtime", None)
        if child_runtime is not None:
            yield from _walk_live_children(child_runtime)


# 2.0.2 review finding 17 (major): keyed by (size, mtime) -- see
# `_agent_log_nodes`'s own docstring. A plain dict/lock (never an LRU
# cap): one entry per sub-agent log this process has ever looked at,
# which is bounded by how many sub-agents a session actually ran, not
# by poll frequency.
_agent_log_nodes_cache: "dict[str, tuple]" = {}
_agent_log_nodes_cache_lock = threading.Lock()


def _agent_log_nodes(log_path: Path) -> list:
    """A bare, dependency-free read of one `agent-*.jsonl` file's lines
    -- used for a `tool_count`/cost lookup without constructing a real
    `SessionLog` (which also wants to create directories etc.). Missing/
    unreadable file or a malformed line is silently skipped, never
    raised -- this is a best-effort reporting path, not core machinery.

    2.0.2 review finding 17 (major): cached by (size, mtime) -- the
    tasks panel's own 1Hz poll used to re-read and re-parse EVERY sub-
    agent's WHOLE jsonl log on every tick just to count tools, even one
    that hadn't grown since the last tick (the overwhelmingly common
    case once a run has more than a couple of agents -- most are
    already finished). Verified: 20 synthetic ~1MB logs took 0.22s PER
    call before this. A cache HIT (unchanged size/mtime) skips the
    read+parse entirely; a MISS (grown, shrunk, or never seen) still
    re-reads the whole file -- not fully incremental, but the dominant
    real-world cost (re-scanning FINISHED agents' static logs every
    tick) is gone."""
    key = str(log_path)
    try:
        st = log_path.stat()
        stamp = (st.st_size, st.st_mtime)
    except OSError:
        with _agent_log_nodes_cache_lock:
            _agent_log_nodes_cache.pop(key, None)
        return []
    with _agent_log_nodes_cache_lock:
        cached = _agent_log_nodes_cache.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    nodes: list = []
    try:
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    nodes.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    with _agent_log_nodes_cache_lock:
        _agent_log_nodes_cache[key] = (stamp, nodes)
    return nodes


def _tool_count_from_log(nodes: list) -> int:
    count = 0
    for node in nodes:
        if node.get("type") != "assistant":
            continue
        for block in (node.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                count += 1
    return count


def _cost_for_agent_from_log(nodes: list, agent_id: str) -> "Optional[float]":
    cost = None
    for node in nodes:
        if node.get("type") == "usage" and node.get("agent_id") == agent_id:
            c = node.get("cost_usd")
            if isinstance(c, (int, float)) and not isinstance(c, bool):
                cost = c
    return cost


def list_agent_task_rows(session) -> list:
    """Halo 2.0.2 round 3 (brief C): every running, queued, background
    and finished sub-agent of THIS session -- the tasks panel's own data
    source (`Controller.list_agent_tasks()`). A flat list of row dicts,
    tree position included (`depth`/`parent_agent_id`, derived from the
    directory nesting `_child_log_paths` already produces, never from
    cross-referencing tool_use ids -- see this function's own `rel_parts`
    math): {agent_id, task_id, title, model, status, is_error, tool_count,
    cost_usd, started, elapsed_s, depth, parent_agent_id, log_path}.
    Reads disk (meta.json + each child's own jsonl log) rather than any
    in-memory event history, so it reports correctly even for a `-c`-
    resumed session that never re-ran a single turn yet; a still-RUNNING
    child's cost is read live off its own `cost_meter` (via `runtime.
    live_children`, walked recursively) when reachable, falling back to
    whatever its own parent's log already rolled up (the usual place a
    FINISHED child's cost lives -- `_rollup_child_cost_into_parent`)."""
    top_subdir = session.log.dir / session.log.session_id / "subagents"
    rows: list = []
    if not top_subdir.is_dir():
        return rows
    live_by_id: dict = {}
    runtime = getattr(session, "agent_runtime", None)
    if runtime is not None:
        live_by_id = dict(_walk_live_children(runtime))
    # 2.0.2 review finding 25: `Path.__lt__` compares the PARTS tuple, not
    # the joined string -- `("agent-P", "subagents", "agent-C.meta.json")`
    # sorts BEFORE `("agent-P.meta.json",)` (a tuple whose first element,
    # the bare folder name "agent-P", is a strict PREFIX of the sibling's
    # first element "agent-P.meta.json", and a prefix always sorts before
    # the longer string it prefixes) -- every child's own meta.json used
    # to sort ahead of its own parent's, and siblings sorted by whatever
    # order their random agent_ids happened to fall in. Collected here in
    # whatever order `rglob` yields (not meaningful either way) and
    # reordered into parent-before-children, `started`-ordered siblings
    # right before returning, below.
    for meta_path in top_subdir.rglob("agent-*.meta.json"):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        agent_id = data.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        rel_parts = meta_path.relative_to(top_subdir).parts
        depth = (len(rel_parts) - 1) // 2
        parent_agent_id = None
        parent_log_path = None
        if len(rel_parts) >= 3:
            parent_dir = meta_path.parent.parent  # the "agent-<parent>" folder itself
            parent_agent_id = parent_dir.name[len("agent-"):]
            parent_log_path = parent_dir.parent / f"{parent_dir.name}.jsonl"
        log_name = meta_path.name
        if log_name.endswith(".meta.json"):
            log_name = log_name[: -len(".meta.json")] + ".jsonl"
        log_path = meta_path.parent / log_name
        nodes = _agent_log_nodes(log_path)
        started = data.get("started")
        finished = data.get("finished")
        elapsed = None
        if isinstance(started, (int, float)):
            end = finished if isinstance(finished, (int, float)) else time.time()
            elapsed = max(0.0, end - started)
        cost = None
        live_child = live_by_id.get(agent_id)
        if live_child is not None:
            cm = getattr(live_child, "cost_meter", None)
            if cm is not None and getattr(cm, "has_cost_data", False):
                cost = cm.total_usd
        if cost is None:
            source_nodes = session.log.nodes() if parent_log_path is None else _agent_log_nodes(parent_log_path)
            cost = _cost_for_agent_from_log(source_nodes, agent_id)
        rows.append({
            "agent_id": agent_id, "task_id": data.get("task_id"), "title": data.get("type") or "?",
            "model": data.get("model"), "status": data.get("status") or "running",
            "is_error": bool(data.get("is_error")), "tool_count": _tool_count_from_log(nodes),
            "cost_usd": cost, "started": started, "elapsed_s": elapsed, "depth": depth,
            "parent_agent_id": parent_agent_id, "log_path": str(log_path),
        })
    return _order_task_rows_by_tree(rows)


def _order_task_rows_by_tree(rows: list) -> list:
    """finding 25: parent-before-children, pre-order; siblings (and every
    root) ordered by `started` (missing/non-numeric sorts last, agent_id
    as a stable tiebreak so the order is deterministic run to run)."""
    by_parent: "dict[Optional[str], list]" = {}
    for row in rows:
        by_parent.setdefault(row.get("parent_agent_id"), []).append(row)

    def _sort_key(row: dict):
        started = row.get("started")
        return (0, started) if isinstance(started, (int, float)) else (1, row["agent_id"])
    for children in by_parent.values():
        children.sort(key=_sort_key)

    ordered: list = []
    seen: set = set()

    def _walk(row: dict) -> None:
        if row["agent_id"] in seen:
            return  # a malformed/cyclic parent_agent_id must never infinite-loop this
        seen.add(row["agent_id"])
        ordered.append(row)
        for child in by_parent.get(row["agent_id"], []):
            _walk(child)
    for root in by_parent.get(None, []):
        _walk(root)
    # Any row whose own parent_agent_id didn't match a REAL row in this
    # same batch (the parent finished and its meta.json was cleaned up
    # mid-scan, or any other inconsistency) is still shown, appended at
    # the end rather than silently dropped.
    for row in rows:
        if row["agent_id"] not in seen:
            _walk(row)
    return ordered

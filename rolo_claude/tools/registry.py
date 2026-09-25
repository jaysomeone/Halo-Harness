"""rolo_claude.tools.registry -- ToolRegistry (H2 scope A): name-sorted
tool defs for the wire (a stable prefix so provider prompt caching isn't
invalidated turn to turn -- rule 4/D5), dispatch by name, a bounded
thread pool for a batch of CONSECUTIVE read-only calls (results returned in
call order, not completion order), and `.without`/`.filtered` for the
catalog-freezing step session start applies (bare-name deny/`--disallowedTools`
removal, `--tools`) -- rolo_claude/permissions.py decides WHICH names;
this module only knows how to build a registry that omits/keeps them.
"""

from __future__ import annotations

import dataclasses
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeoutError
from typing import Optional

from rolo_claude.agent.planmode import EnterPlanModeTool, ExitPlanModeTool
from rolo_claude.tools.agent import AgentTool, TaskTool
from rolo_claude.tools.ask_user_question import AskUserQuestionTool
from rolo_claude.tools.base import Tool, ToolContext, ToolResult
from rolo_claude.tools.bash import BashTool
from rolo_claude.tools.bash_output import BashOutputTool
from rolo_claude.tools.edit import EditTool
from rolo_claude.tools.glob_tool import GlobTool
from rolo_claude.tools.grep_tool import GrepTool
from rolo_claude.tools.notebook_edit import NotebookEditTool
from rolo_claude.tools.read import ReadTool
from rolo_claude.tools.skill import SkillTool
from rolo_claude.tools.task_stop import TaskStopTool
from rolo_claude.tools.todowrite import TodoWriteTool
from rolo_claude.tools.tool_search import ToolSearchTool
from rolo_claude.tools.webfetch import WebFetchTool
from rolo_claude.tools.write import WriteTool

READ_ONLY_POOL_SIZE = 4
# H3 must-do: if an MCP `readOnlyHint` tool ever joins this pool, one hung
# call (finding 8's FIFO scenario, or any other unexpected block) must
# never hold up the whole turn -- every future is awaited with this
# per-call bound, abort-aware.
READ_ONLY_CALL_TIMEOUT_S = 30.0


def default_tools() -> list:
    """Every built-in tool for this platform. PowerShell is win32-only
    (imported lazily so a POSIX/Kali process never even imports a module
    that assumes `powershell.exe` might exist)."""
    tools = [
        AgentTool(), AskUserQuestionTool(), BashTool(), BashOutputTool(), EditTool(), EnterPlanModeTool(),
        ExitPlanModeTool(), GlobTool(), GrepTool(), NotebookEditTool(), ReadTool(), SkillTool(), TaskStopTool(),
        TaskTool(), TodoWriteTool(), ToolSearchTool(), WebFetchTool(), WriteTool(),
    ]
    if sys.platform == "win32":
        from rolo_claude.tools.powershell import PowerShellTool
        tools.append(PowerShellTool())
    return tools


class ToolRegistry:
    def __init__(self, tools: Optional[list] = None):
        self._tools: dict = {t.name: t for t in (tools if tools is not None else default_tools())}

    def definitions(self) -> list:
        """Anthropic tool-def dicts, name-sorted."""
        return [self._tools[name].definition() for name in sorted(self._tools)]

    def definitions_for(self, names: list) -> list:
        """Anthropic tool-def dicts for EXACTLY `names`, in the GIVEN
        order (never re-sorted) -- H3 scope C: `agent.catalog.
        SessionCatalog`'s growing wire-catalog name list is append-only,
        never reordered, so the defs it logs must preserve that order too
        (unlike `.definitions()`, which is only ever right for the ONE
        initial freeze, before anything can have grown). A name with no
        matching tool is silently skipped rather than raising -- a stale
        name surviving an LRU eviction race is a shrink, not a crash."""
        return [self._tools[n].definition() for n in names if n in self._tools]

    def names(self) -> list:
        return sorted(self._tools)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def summary(self, name: str, input: dict) -> str:
        tool = self.get(name)
        return tool.summary(input) if tool else f"{name}(...)"

    def is_read_only(self, name: str) -> bool:
        tool = self.get(name)
        return bool(tool.is_read_only) if tool else False

    def result_cap(self, name: str):
        tool = self.get(name)
        return tool.result_cap if tool else None

    def without(self, names) -> "ToolRegistry":
        """A NEW registry with `names` removed (session-start catalog
        freezing: bare-name deny rules / --disallowedTools)."""
        names = set(names)
        return ToolRegistry(tools=[t for n, t in self._tools.items() if n not in names])

    def add_tool(self, tool: Tool) -> None:
        """H3 scope C: the ONE deliberate in-place mutation this otherwise-
        immutable-style class allows -- `agent/catalog.py`'s lazy-load path
        (ToolSearch loading a deferred MCP tool) adds it to the SAME
        registry instance a session's whole `ToolContext.registry`/
        `Session.tool_registry` already points at, so every later
        `dispatch`/`is_read_only`/`result_cap` call sees it immediately
        with no extra plumbing. Never used at session-freeze time (that
        path stays `without`/`filtered`, which return new registries)."""
        self._tools[tool.name] = tool

    def remove_tool(self, name: str) -> None:
        """The inverse of `add_tool` -- LRU eviction of a previously
        lazy-loaded deferred tool. A no-op if `name` isn't present."""
        self._tools.pop(name, None)

    def filtered(self, names) -> "ToolRegistry":
        """A NEW registry containing ONLY `names` that exist (`--tools
        Bash,Edit,Read`); an empty `names` yields an EMPTY registry
        (`--tools ""`)."""
        names = set(names)
        return ToolRegistry(tools=[t for n, t in self._tools.items() if n in names])

    def dispatch(self, name: str, input: dict, ctx: ToolContext) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool: {name!r}. Available tools: {', '.join(self.names())}", is_error=True)
        # H10 Part A: `duration_ms` wraps every tool call -- solo AND
        # batched (run_read_only_batch below calls THIS same method per
        # call, on its own worker thread) -- the one choke point every
        # dispatch goes through, so telemetry's `tool_result.ms` never
        # needs its own per-tool instrumentation.
        t0 = time.monotonic()
        try:
            result = tool.run(input if isinstance(input, dict) else {}, ctx)
        except Exception as e:  # a tool must never crash the loop
            result = ToolResult(f"Tool {name!r} raised {type(e).__name__}: {e}", is_error=True)
        result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
        return result


def run_read_only_batch(registry: ToolRegistry, calls: list, ctx: ToolContext) -> list:
    """`calls` is `[(name, input), ...]` or `[(name, input, tool_use_id), ...]`
    (finding 5 must-do: a pooled call gets its OWN `tool_use_id`, so an
    MCP result that needs to spill lands at `<session_dir>/tool-results/
    <tool_use_id>.txt` like any other, instead of every call in the batch
    silently sharing one `ctx` with `tool_use_id=None`) -- all of which
    the CALLER has already established are read-only and safe to run
    concurrently. Runs them on a bounded thread pool (`READ_ONLY_POOL_SIZE`)
    and returns `ToolResult`s in the SAME order as `calls`, independent of
    which one actually finished first.

    H3 must-do: abort-aware and bounded per call (`READ_ONLY_CALL_TIMEOUT_S`)
    -- collecting each future's result polls in short slices instead of a
    single blocking `.result()`, so a hung call (finding 8's unfixed-FIFO
    scenario, or anything else that blocks unexpectedly) gets a clear
    timeout/aborted `ToolResult` instead of freezing the whole turn, and
    the session's `abort` Event cuts the wait short too. finding 4 must-do:
    this now includes a SOLO call too (the old fast path dispatched it
    directly, bypassing this exact wait) -- "route single read-only calls
    through the same wait". The pool itself is shut down WITHOUT waiting
    (`wait=False`) so a still-stuck call's own worker thread (Python cannot
    forcibly kill a running thread) is simply abandoned rather than
    blocking this function's return."""
    if not calls:
        return []

    def _ctx_for(tool_use_id: Optional[str]) -> ToolContext:
        return dataclasses.replace(ctx, tool_use_id=tool_use_id) if tool_use_id is not None else ctx

    abort = getattr(ctx, "abort", None)
    pool = ThreadPoolExecutor(max_workers=min(READ_ONLY_POOL_SIZE, len(calls)))
    try:
        futures = []
        for entry in calls:
            name, tool_input, tool_use_id = entry if len(entry) == 3 else (*entry, None)
            futures.append(pool.submit(registry.dispatch, name, tool_input, _ctx_for(tool_use_id)))
        results = []
        for f in futures:
            deadline = time.monotonic() + READ_ONLY_CALL_TIMEOUT_S
            result = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result = ToolResult(
                        f"Tool call timed out after {READ_ONLY_CALL_TIMEOUT_S:.0f}s waiting in the "
                        f"read-only pool.", is_error=True,
                    )
                    break
                if abort is not None and abort.is_set():
                    result = ToolResult("Tool call aborted while waiting in the read-only pool.", is_error=True)
                    break
                try:
                    result = f.result(timeout=min(0.2, remaining))
                    break
                except _FutureTimeoutError:
                    continue
            results.append(result)
        return results
    finally:
        pool.shutdown(wait=False)

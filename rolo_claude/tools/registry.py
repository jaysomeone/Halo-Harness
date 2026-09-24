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

import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from rolo_claude.tools.ask_user_question import AskUserQuestionTool
from rolo_claude.tools.base import Tool, ToolContext, ToolResult
from rolo_claude.tools.bash import BashTool
from rolo_claude.tools.edit import EditTool
from rolo_claude.tools.glob_tool import GlobTool
from rolo_claude.tools.grep_tool import GrepTool
from rolo_claude.tools.read import ReadTool
from rolo_claude.tools.skill import SkillTool
from rolo_claude.tools.todowrite import TodoWriteTool
from rolo_claude.tools.tool_search import ToolSearchTool
from rolo_claude.tools.webfetch import WebFetchTool
from rolo_claude.tools.write import WriteTool

READ_ONLY_POOL_SIZE = 4


def default_tools() -> list:
    """Every built-in tool for this platform. PowerShell is win32-only
    (imported lazily so a POSIX/Kali process never even imports a module
    that assumes `powershell.exe` might exist)."""
    tools = [
        AskUserQuestionTool(), BashTool(), EditTool(), GlobTool(), GrepTool(),
        ReadTool(), SkillTool(), TodoWriteTool(), ToolSearchTool(), WebFetchTool(), WriteTool(),
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
        try:
            return tool.run(input if isinstance(input, dict) else {}, ctx)
        except Exception as e:  # a tool must never crash the loop
            return ToolResult(f"Tool {name!r} raised {type(e).__name__}: {e}", is_error=True)


def run_read_only_batch(registry: ToolRegistry, calls: list, ctx: ToolContext) -> list:
    """`calls` is `[(name, input), ...]`, all of which the CALLER has
    already established are read-only and safe to run concurrently. Runs
    them on a bounded thread pool (`READ_ONLY_POOL_SIZE`) and returns
    `ToolResult`s in the SAME order as `calls`, independent of which one
    actually finished first."""
    if not calls:
        return []
    if len(calls) == 1:
        name, tool_input = calls[0]
        return [registry.dispatch(name, tool_input, ctx)]
    with ThreadPoolExecutor(max_workers=min(READ_ONLY_POOL_SIZE, len(calls))) as pool:
        futures = [pool.submit(registry.dispatch, name, tool_input, ctx) for name, tool_input in calls]
        return [f.result() for f in futures]

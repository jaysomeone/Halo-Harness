"""rolo_claude.tools.registry -- ToolRegistry (H1 scope G): name-sorted
tool defs for the wire (a stable prefix so provider prompt caching isn't
invalidated turn to turn -- rule 4/D5) and dispatch by name.
"""

from __future__ import annotations

from typing import Optional

from rolo_claude.tools.base import Tool, ToolContext, ToolResult
from rolo_claude.tools.read import ReadTool


class ToolRegistry:
    def __init__(self, tools: Optional[list] = None):
        self._tools: dict = {t.name: t for t in (tools if tools is not None else [ReadTool()])}

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

    def dispatch(self, name: str, input: dict, ctx: ToolContext) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool: {name!r}. Available tools: {', '.join(self.names())}", is_error=True)
        try:
            return tool.run(input if isinstance(input, dict) else {}, ctx)
        except Exception as e:  # a tool must never crash the loop
            return ToolResult(f"Tool {name!r} raised {type(e).__name__}: {e}", is_error=True)

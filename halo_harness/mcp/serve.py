"""halo_harness.mcp.serve -- `halo mcp serve`: Halo's own built-in tools as
a standalone stdio MCP server (Halo 2.0.1 gap-list brief, W4b item 2:
"expose Halo's built-in tools as an MCP server; the ccbridge code is the
base"). Same `mcp.server.lowlevel.Server` + `mcp.server.stdio.stdio_server()`
pattern `ccbridge/__main__.py` already uses for the `cc:` route's own
bridge, minus the parent-process socket indirection -- this process already
HAS the tool registry in-proc, so `on_call_tool` dispatches straight into
it.

Exposes a curated, STATELESS-safe subset of `tools.registry.default_tools()`
-- never Agent/Task/AskUserQuestion/EnterPlanMode/ExitPlanMode/TaskStop/
BashOutput/ToolSearch, every one of which needs a live session/agent-runtime
or background-job registry this standalone process doesn't have.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_SERVE_TOOL_NAMES = ("Bash", "Edit", "Glob", "Grep", "NotebookEdit", "Read", "TodoWrite", "WebFetch", "Write")


def build_serve_registry():
    from halo_harness.tools.registry import ToolRegistry, default_tools
    names = set(_SERVE_TOOL_NAMES)
    if sys.platform == "win32":
        names.add("PowerShell")
    return ToolRegistry([t for t in default_tools() if t.name in names])


def _tool_from_def(d: dict, types_mod):
    return types_mod.Tool(name=d["name"], description=d.get("description", ""),
                           input_schema=d.get("input_schema") or {"type": "object", "properties": {}})


def _content_blocks(content, types_mod) -> list:
    blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
    out = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "image":
            src = b.get("source") or {}
            out.append(types_mod.ImageContent(type="image", data=src.get("data", ""),
                                                mime_type=src.get("media_type") or "image/png"))
        elif isinstance(b, dict):
            out.append(types_mod.TextContent(type="text", text=b.get("text", "")))
        else:
            out.append(types_mod.TextContent(type="text", text=str(b)))
    return out or [types_mod.TextContent(type="text", text="")]


async def run_server(cwd: Path, *, env: Optional[dict] = None) -> None:
    import mcp.server.stdio
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from halo_harness.tools.base import ToolContext

    registry = build_serve_registry()
    ctx = ToolContext(cwd=cwd, env=env)

    async def on_list_tools(_ctx, _params):
        return types.ListToolsResult(tools=[_tool_from_def(d, types) for d in registry.definitions()])

    async def on_call_tool(_ctx, params):
        import asyncio
        try:
            result = await asyncio.to_thread(registry.dispatch, params.name, params.arguments or {}, ctx)
        except Exception as e:  # a tool crash must never take this server process down
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"halo mcp serve: {type(e).__name__}: {e}")],
                is_error=True,
            )
        return types.CallToolResult(content=_content_blocks(result.content, types), is_error=bool(result.is_error))

    server = Server("halo", version="1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_options)


def main(argv=None) -> int:
    """`python -m halo_harness.mcp.serve [cwd]` -- a direct entry point
    (alongside `halo mcp serve`) so tests can spawn this exactly as any
    other real MCP server subprocess (see tests/helpers/fake_mcp_server.py's
    own convention) and connect to it as a real client."""
    import asyncio
    argv = sys.argv[1:] if argv is None else argv
    cwd = Path(argv[0]).resolve() if argv else Path.cwd()
    asyncio.run(run_server(cwd))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""python -m rolo_claude.ccbridge -- the CHILD side of the tool bridge
(H11 Part B): a real stdio MCP server (the official `mcp` SDK) that
`claude` itself spawns via `--mcp-config` (named "rolo", so Claude Code
exposes every bridged tool as `mcp__rolo__<Name>`). Forwards `tools/list`/
`tools/call` to the PARENT rolo-claude process's ToolBridgeServer over a
local socket (`ccbridge.client.ParentLink`, wired from the environment --
see server.py's `child_env()`). Every actual dispatch (permission decide,
hooks, the tool's own run, the session log) happens on the PARENT side
(`agent/cc_runtime.py`); this process is a thin, stateless relay.
"""

from __future__ import annotations

import asyncio
import sys

from rolo_claude.ccbridge.client import ParentLink, ParentLinkError


def _content_blocks(raw_blocks, types_mod):
    out = []
    for b in raw_blocks or []:
        if isinstance(b, dict) and b.get("type") == "image":
            out.append(types_mod.ImageContent(type="image", data=b.get("data", ""),
                                                mime_type=b.get("mime_type") or "image/png"))
        elif isinstance(b, dict):
            out.append(types_mod.TextContent(type="text", text=b.get("text", "")))
        else:
            out.append(types_mod.TextContent(type="text", text=str(b)))
    if not out:
        out.append(types_mod.TextContent(type="text", text=""))
    return out


async def _run(link: ParentLink) -> None:
    import mcp.server.stdio
    import mcp.types as types
    from mcp.server.lowlevel import Server

    async def on_list_tools(ctx, params):
        tools = await asyncio.to_thread(link.list_tools)
        return types.ListToolsResult(tools=[
            types.Tool(
                name=t.get("name", ""), description=t.get("description", ""),
                input_schema=t.get("input_schema") or {"type": "object", "properties": {}},
            )
            for t in tools if isinstance(t, dict) and t.get("name")
        ])

    async def on_call_tool(ctx, params):
        try:
            result = await asyncio.to_thread(link.call_tool, params.name, params.arguments or {})
        except (ParentLinkError, Exception) as e:  # never let a link/relay error kill the MCP server process
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"ccbridge: {type(e).__name__}: {e}")],
                is_error=True,
            )
        return types.CallToolResult(
            content=_content_blocks(result.get("content"), types),
            is_error=bool(result.get("is_error")),
        )

    server = Server("rolo", version="1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv=None) -> int:
    try:
        link = ParentLink()
    except ParentLinkError as e:
        print(f"rolo-claude ccbridge: {e}", file=sys.stderr)
        return 1
    try:
        asyncio.run(_run(link))
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

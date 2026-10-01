"""python -m rolo_claude.ccbridge -- the CHILD side of the tool bridge
(H11 Part B): a real stdio MCP server (the official `mcp` SDK) that
`claude` itself spawns via `--mcp-config` (named "rolo", so Claude Code
exposes every bridged tool as `mcp__rolo__<Name>`). Forwards `tools/list`/
`tools/call` to the PARENT rolo-claude process's ToolBridgeServer over a
local socket (`ccbridge.client.ParentLink`, wired from the environment --
see server.py's `child_env()`). Every actual dispatch (permission decide,
hooks, the tool's own run, the session log) happens on the PARENT side
(`agent/cc_runtime.py`); this process is a thin, stateless relay.

H11b finding 6: also runs a background watcher (a SECOND `ParentLink`
connection, never sharing the one `tools/list`/`tools/call` use, so a
long-poll never delays a real tool call) that long-polls the parent's
`tools/await_change` and calls the MCP session's own `send_tool_list_
changed()` the moment a ToolSearch load actually grows the catalog --
without this, Claude Code only ever lists tools once, at startup, and can
never learn a newly-loaded deferred tool exists.
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


def _tool_from_dict(t: dict, types_mod):
    """finding 7: forwards `annotations.readOnlyHint`/`destructiveHint`
    and `_meta` when `bridge_list_tools` (agent/cc_runtime.py) included
    them -- lets Claude Code apply its own per-tool result-size override
    (`_meta["anthropic/maxResultSizeChars"]`) instead of its generic 25k-
    token cap, and tell a read-only bridged tool apart from a mutating
    one the same way it would for a native MCP server."""
    annotations = None
    raw_ann = t.get("annotations")
    if isinstance(raw_ann, dict):
        annotations = types_mod.ToolAnnotations(
            read_only_hint=raw_ann.get("readOnlyHint"), destructive_hint=raw_ann.get("destructiveHint"),
        )
    kwargs = dict(name=t.get("name", ""), description=t.get("description", ""),
                   input_schema=t.get("input_schema") or {"type": "object", "properties": {}})
    if annotations is not None:
        kwargs["annotations"] = annotations
    meta = t.get("_meta")
    if isinstance(meta, dict) and meta:
        kwargs["meta"] = meta
    return types_mod.Tool(**kwargs)


async def _watch_tool_changes(session) -> None:
    """Runs for the lifetime of this child process (cancelled, best-
    effort, when `_run` returns). A fresh `ParentLink` of its own --
    never the one `on_list_tools`/`on_call_tool` share."""
    try:
        link = ParentLink()
    except ParentLinkError:
        return
    known_generation = 0
    try:
        while True:
            try:
                resp = await asyncio.to_thread(link.await_tools_change, known_generation)
            except ParentLinkError:
                return
            new_generation = resp.get("generation")
            if isinstance(new_generation, int) and new_generation != known_generation:
                known_generation = new_generation
                try:
                    await session.send_tool_list_changed()
                except Exception:
                    pass
    finally:
        link.close()


async def _run(link: ParentLink) -> None:
    import mcp.server.stdio
    import mcp.types as types
    from mcp.server.lowlevel import NotificationOptions, Server

    watch_task: "asyncio.Task | None" = None

    async def on_list_tools(ctx, params):
        nonlocal watch_task
        tools = await asyncio.to_thread(link.list_tools)
        if watch_task is None:
            watch_task = asyncio.create_task(_watch_tool_changes(ctx.session))
        return types.ListToolsResult(tools=[
            _tool_from_dict(t, types) for t in tools if isinstance(t, dict) and t.get("name")
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
    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            init_options = server.create_initialization_options(notification_options=NotificationOptions(
                tools_changed=True))
            await server.run(read_stream, write_stream, init_options)
    finally:
        if watch_task is not None:
            watch_task.cancel()


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

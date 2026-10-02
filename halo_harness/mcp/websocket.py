"""halo_harness.mcp.websocket -- `ws`/`websocket` transport connect (Halo
2.0.1 gap-list brief, W4 unbuilt surfaces: "transports ws/websocket via the
SDK's websocket client when available").

UNVERIFIED, by necessity: the pinned `mcp==2.2.0` this project ships with
has no `mcp.client.websocket` module at all (confirmed: `importlib.util.
find_spec("mcp.client.websocket")` is `None` -- see `manager.
websocket_client_available()`), so this code path is NEVER exercised
against a real SDK anywhere in this tree -- `manager.parse_server` only
ever reaches it once a FUTURE `mcp` version actually ships one. The call
shape below (`websocket_client(url)` yielding `(read_stream, write_stream)`)
mirrors every other transport this SDK already has (`sse_client`,
`streamable_http_client`) and the MCP spec's own reference client, but it
is a best guess, not something verified live the way every OTHER transport
in this package is. Whoever upgrades past 2.2.0 should re-verify this
against the real API before relying on it, the same way http_sse.py's own
docstrings record what WAS verified against 2.2.0.
"""

from __future__ import annotations

from contextlib import AsyncExitStack


async def connect_websocket(*, url: str, connect_timeout: float):
    """Returns `(stack, session)`, stack OPEN -- same contract as
    `stdio.connect`/`http_sse.connect_http`/`connect_sse`. Headers are not
    passed: a browser-shaped WebSocket handshake generally can't carry
    custom headers either, which is why MCP's own websocket transport (per
    the spec) relies on the URL/subprotocol alone, not an `Authorization`
    header -- the same limitation real websocket-based MCP clients live
    with."""
    from mcp import ClientSession
    from mcp.client.websocket import websocket_client
    from halo_harness.mcp.client import task_timeout

    stack = AsyncExitStack()
    try:
        async with task_timeout(connect_timeout):
            read, write = await stack.enter_async_context(websocket_client(url))
        session = await stack.enter_async_context(ClientSession(read, write))
        return stack, session
    except BaseException:
        await stack.aclose()
        raise

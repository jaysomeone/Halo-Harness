"""tests.helpers.fake_mcp_server -- a real MCP stdio server (JSON-RPC 2.0
over the SDK's own wire format, via `mcp.server.mcpserver.MCPServer`) for
H3's MCP-client tests. Run directly: `python -m tests.helpers.fake_mcp_server
[mode]` (or as a module path via `-m tests.helpers.fake_mcp_server`); modes
below via `FAKE_MCP_MODE` env var (or argv[1]).

Tools (always present): `echo` (text passthrough), `image` (one 1x1 PNG as
an ImageContent block), `error_tool` (always isError), `huge` (a text blob
sized well past MAX_MCP_OUTPUT_TOKENS*4, to exercise output-cap
truncation), `slow_tool` (sleeps `args["seconds"]`, for per-call timeout
tests), `always_load_tool` (`_meta={"anthropic/alwaysLoad": True}`),
`read_only_tool` (`annotations.read_only_hint=True`). Plus
`FAKE_MCP_TOOL_COUNT` (env var, default 5; the brief's own "5 or 300")
total tools -- filler tools `filler_0..N` pad the count up when > 7.

One resource (`fake://note`) and one prompt (`greet`) are registered too.

Modes (`FAKE_MCP_MODE` env, default "normal"):
  - normal:      everything above, works.
  - slow:        sleeps `FAKE_MCP_SLEEP_S` (default 2.0) seconds before
                 completing startup -- pairs with a short `MCP_TIMEOUT` in
                 the CLIENT test to deterministically produce "failed".
  - crash:       exits(1) immediately, before ever accepting a connection
                 -- the client sees a connect failure.
  - needs-auth:  exits(1) with a stderr line containing "401 Unauthorized"
                 -- stdio has no real auth concept (MCP auth/OAuth is an
                 HTTP-transport thing), so from the CLIENT's side this is
                 observably a connect failure exactly like "crash"; the
                 manager's needs_auth STATE TRANSITION itself is covered by
                 a direct unit test that injects a synthetic 401-shaped
                 exception rather than relying on a stdio process to
                 produce one (documented in test_mcp_manager.py).
"""

from __future__ import annotations

import base64
import os
import sys
import time
from contextlib import contextmanager

# A real (tiny, valid) 1x1 transparent PNG, base64-encoded.
_PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@contextmanager
def running_manager(configs: dict, **kwargs):
    """`McpManager(configs, **kwargs)`, `start_all()`-ed, yielded, and
    ALWAYS `close_all()`-ed on the way out -- including when the caller's
    own `with` block raises -- so a test can't accidentally skip cleanup
    and leave a live fake-server subprocess + McpLoop thread behind for
    interpreter shutdown to trip over (see rolo_claude/mcp/client.py's
    `McpLoop.stop()`/atexit safety net for the belt-and-suspenders half of
    this; this is the suspenders). One-line replacement for the
    hand-written `mgr = McpManager(...); try: mgr.start_all(); ...;
    finally: mgr.close_all()` shape most tests in this file already use."""
    from rolo_claude.mcp.manager import McpManager
    mgr = McpManager(configs, **kwargs)
    try:
        mgr.start_all()
        yield mgr
    finally:
        mgr.close_all()


def _tool_count() -> int:
    try:
        return max(1, int(os.environ.get("FAKE_MCP_TOOL_COUNT", "5")))
    except ValueError:
        return 5


def build_app():
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    app = MCPServer(name="fake-mcp-server", instructions="A fake MCP server for rolo-claude's own tests.")

    @app.tool(name="echo", description="Echo back the given text.")
    def echo(text: str = "") -> str:
        return text

    @app.tool(name="image", description="Return a tiny 1x1 PNG image.")
    def image() -> "list":
        from mcp.types import ImageContent
        return [ImageContent(type="image", data=_PNG_1X1, mime_type="image/png")]

    @app.tool(name="error_tool", description="Always raises (isError=True).")
    def error_tool() -> str:
        raise ValueError("error_tool always fails, on purpose")

    @app.tool(name="huge", description="Return a text blob larger than any output cap.")
    def huge(chars: int = 200_000) -> str:
        return "X" * max(1, min(chars, 2_000_000))

    @app.tool(name="slow_tool", description="Sleep `seconds` before replying.")
    def slow_tool(seconds: float = 1.0) -> str:
        time.sleep(max(0.0, min(seconds, 30.0)))
        return f"slept {seconds}s"

    @app.tool(name="always_load_tool", description="Marked _meta.anthropic/alwaysLoad.",
              meta={"anthropic/alwaysLoad": True})
    def always_load_tool() -> str:
        return "always-loaded"

    @app.tool(name="read_only_tool", description="Marked annotations.readOnlyHint.",
              annotations=ToolAnnotations(read_only_hint=True))
    def read_only_tool() -> str:
        return "read-only result"

    core_names = {"echo", "image", "error_tool", "huge", "slow_tool", "always_load_tool", "read_only_tool"}
    n = _tool_count()
    filler_needed = max(0, n - len(core_names))
    for i in range(filler_needed):
        def _make(i=i):
            def _filler() -> str:
                return f"filler {i} result"
            return _filler
        app.add_tool(_make(), name=f"filler_{i}", description=f"Filler tool #{i} (scale/cap testing).")

    @app.resource("fake://note")
    def note() -> str:
        return "This is a fake MCP resource's text content."

    @app.prompt(name="greet")
    def greet(name: str = "world") -> str:
        return f"Say hello to {name}."

    return app


def main() -> None:
    mode = os.environ.get("FAKE_MCP_MODE") or (sys.argv[1] if len(sys.argv) > 1 else "normal")
    if mode == "crash":
        sys.stderr.write("fake_mcp_server: crash mode -- exiting immediately\n")
        sys.exit(1)
    if mode == "needs-auth":
        sys.stderr.write("fake_mcp_server: 401 Unauthorized (needs-auth mode)\n")
        sys.exit(1)
    if mode == "slow":
        time.sleep(float(os.environ.get("FAKE_MCP_SLEEP_S", "2.0")))

    app = build_app()
    app.run(transport="stdio")


if __name__ == "__main__":
    main()

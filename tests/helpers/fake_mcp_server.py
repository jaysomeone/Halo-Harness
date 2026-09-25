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

`FAKE_MCP_EXTRA_TOOLS` (comma-separated, opt-in, never present by default)
also adds: `badbytes_tool`, `die_mid_call` (see below), and four
unusual-schema tools for H9 Part B item 9 (each a real callable that
echoes its input back as JSON, with its wire `input_schema` overwritten
to an exact hand-built shape via the tool manager's own `.parameters`):
`ref_defs_tool` ($ref/$defs), `anyof3_tool` (3-way anyOf with no shared
type), `tuple_legacy_tool` (draft-04/07 tuple `items` as an array, the
form current pydantic no longer emits on its own), `deep_nested_tool`
(5 levels of inline object nesting plus a `pattern` property name).

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
import json
import os
import sys
import time
from contextlib import contextmanager
from typing import Any

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
    # `progress_slow_tool` below (opt-in) type-hints a `Context` parameter;
    # this module's own `from __future__ import annotations` stringifies
    # that hint, and the SDK's own signature introspection later `eval()`s
    # it against the DEFINING FUNCTION's `__globals__` -- i.e. THIS
    # module's globals, never build_app()'s own locals, however this name
    # got bound here. `global` makes the import below land there instead
    # of as a function-local (every other `mcp` import in this function
    # stays local -- ToolAnnotations/ImageContent are only ever used as
    # plain runtime values, never as a string annotation needing later
    # resolution, so they have no such requirement).
    global Context
    from mcp.server.mcpserver import Context

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

    # finding 16 test 3: opt-in via FAKE_MCP_EXTRA_TOOLS (comma-separated),
    # never present by default -- every EXISTING test's tool count/set stays
    # exactly as before.
    extra = {e.strip() for e in (os.environ.get("FAKE_MCP_EXTRA_TOOLS") or "").split(",") if e.strip()}
    extra_added = 0
    if "badbytes_tool" in extra:
        @app.tool(name="badbytes_tool",
                  description="Emit raw invalid UTF-8 bytes to stdout before answering (finding 3 repro).")
        def badbytes_tool() -> str:
            try:
                sys.stdout.buffer.write(b"\xff\xfeA\n")
                sys.stdout.buffer.flush()
            except Exception:
                pass
            return "answered after bad bytes"
        extra_added += 1
    if "die_mid_call" in extra:
        @app.tool(name="die_mid_call",
                  description="Hard-exit the process immediately, no cleanup (finding 6 repro: dead transport mid-call).")
        def die_mid_call() -> str:
            os._exit(1)
        extra_added += 1
    if "progress_slow_tool" in extra:
        @app.tool(name="progress_slow_tool",
                  description="Sleeps in steps, sending a REAL progress notification after each one "
                              "(OpenCode-H9 MCP-compatibility item: progress resets the call timeout).")
        async def progress_slow_tool(ctx: Context, steps: int = 3, seconds_per_step: float = 0.15) -> str:
            import asyncio
            steps = max(1, min(int(steps), 20))
            for i in range(steps):
                await asyncio.sleep(max(0.0, min(seconds_per_step, 5.0)))
                await ctx.report_progress(i + 1, steps, "still going")
            return "done after real progress"
        extra_added += 1

    # H9 Part B item 9: unusual JSON-Schema shapes a real MCP server (built
    # with this SAME `mcp` SDK, whose pydantic-backed decorator genuinely
    # produces $ref/$defs/anyOf/enum -- verified by hand against the
    # installed SDK before writing these) can hand back. Each tool is
    # registered normally (a real, callable Python function -- so
    # call_tool still dispatches and echoes its input back as JSON) and
    # then its wire schema is OVERWRITTEN via the tool manager's own
    # `.parameters` dict, which `list_tools()` reads verbatim -- the only
    # way to get an EXACT, deterministic shape (incl. the legacy
    # draft-04/07 tuple-`items`-as-an-array form current pydantic no
    # longer emits on its own, using `prefixItems` instead) without
    # depending on however a particular pydantic version happens to
    # render a given type hint. Opt-in only, same as badbytes_tool/
    # die_mid_call above -- never present by default.
    def _override_schema(name, schema):
        # The REAL underlying function keeps its own loosely-typed
        # signature (below) for pydantic's arg-validation model -- only
        # the WIRE schema (what a client/model actually sees via
        # list_tools()) is overwritten to the exact custom shape. The two
        # need not match key-for-key; this only has to accept whatever
        # arguments the caller's own test actually sends.
        app._tool_manager.get_tool(name).parameters = schema

    if "ref_defs_tool" in extra:
        @app.tool(name="ref_defs_tool", description="Tool with a nested $ref/$defs schema.")
        def ref_defs_tool(address: dict = None, role: str = "user") -> str:
            return json.dumps({"address": address, "role": role}, default=str)
        _override_schema("ref_defs_tool", {
            "type": "object",
            "$defs": {"Address": {
                "type": "object",
                "properties": {"street": {"type": "string"}, "city": {"type": "string"}},
                "required": ["street", "city"],
            }},
            "properties": {
                "address": {"$ref": "#/$defs/Address"},
                "role": {"type": "string", "enum": ["admin", "user", "guest"]},
            },
            "required": ["address"],
        })
        extra_added += 1
    if "anyof3_tool" in extra:
        @app.tool(name="anyof3_tool", description="Tool with a 3-way anyOf (int|string|null), no shared type.")
        def anyof3_tool(value: Any = None) -> str:
            return json.dumps({"value": value}, default=str)
        _override_schema("anyof3_tool", {
            "type": "object",
            "properties": {
                "value": {"anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}]},
            },
        })
        extra_added += 1
    if "tuple_legacy_tool" in extra:
        @app.tool(name="tuple_legacy_tool", description="Tool with a legacy draft-04/07 tuple-items array.")
        def tuple_legacy_tool(point: list = None) -> str:
            return json.dumps({"point": point}, default=str)
        _override_schema("tuple_legacy_tool", {
            "type": "object",
            "properties": {
                "point": {"type": "array", "items": [{"type": "integer"}, {"type": "integer"}, {"type": "string"}],
                           "minItems": 3, "maxItems": 3},
            },
            "required": ["point"],
        })
        extra_added += 1
    if "deep_nested_tool" in extra:
        @app.tool(name="deep_nested_tool", description="Tool with 5 levels of inline (no $ref) object nesting.")
        def deep_nested_tool(level1: dict = None) -> str:
            return json.dumps({"level1": level1}, default=str)
        _override_schema("deep_nested_tool", {
            "type": "object",
            "properties": {"level1": {"type": "object", "properties": {"level2": {"type": "object", "properties": {
                "level3": {"type": "object", "properties": {"level4": {"type": "object", "properties": {
                    "level5": {"type": "array", "items": {"type": "object", "properties": {
                        "leaf": {"type": "string", "pattern": "^[a-z]+$"}}}},
                }}}}}}}}},
        })
        extra_added += 1

    core_names = {"echo", "image", "error_tool", "huge", "slow_tool", "always_load_tool", "read_only_tool"}
    n = _tool_count()
    filler_needed = max(0, n - len(core_names) - extra_added)
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
    pid_file = os.environ.get("FAKE_MCP_PID_FILE")
    if pid_file:
        # finding 16 test 5: written FIRST, before any mode dispatch (incl.
        # "slow"/"crash"), so a caller can find this process's real PID
        # regardless of which mode it's running in.
        try:
            with open(pid_file, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass
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
    # H9 Part B item 12: http/sse transports via a REAL fake server (never
    # exercised for real anywhere else in this suite -- see http_sse.py's
    # own module docstring: "none of rolo's real 15 configured servers use
    # http/sse"). `FAKE_MCP_TRANSPORT` (default "stdio") selects the
    # transport; `FAKE_MCP_PORT` (required for "http"/"sse") is the port to
    # bind on 127.0.0.1 -- the caller picks a free one and polls it, same
    # pattern as tests/helpers/mock_openai.py's own `free_port()`.
    transport = os.environ.get("FAKE_MCP_TRANSPORT", "stdio")
    if transport in ("http", "sse"):
        port = int(os.environ.get("FAKE_MCP_PORT", "0"))
        sdk_transport = "streamable-http" if transport == "http" else "sse"
        app.run(transport=sdk_transport, host="127.0.0.1", port=port)
    else:
        app.run(transport="stdio")


if __name__ == "__main__":
    main()

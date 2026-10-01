"""halo_harness.ccbridge -- H11 Part B: the local tool bridge between a
`cc:`-route halo session and the `claude` subprocess it drives.

Two halves, two different wire protocols:
  * `server.ToolBridgeServer` (PARENT side, runs inside the halo
    process): a tiny newline-JSON RPC server over a Unix socket (POSIX,
    `~/.halo/run/<sid>.sock`, mode 0600) or a TCP loopback socket
    + random per-connection token (Windows) -- see `server.py`'s own
    docstring for the exact wire shape.
  * `python -m halo_harness.ccbridge` (CHILD side, spawned BY `claude`
    itself via `--mcp-config`, named "rolo" so Claude Code exposes every
    bridged tool as `mcp__rolo__<Name>`): a real stdio MCP server (the
    official `mcp` SDK) that forwards `tools/list`/`tools/call` to the
    PARENT over the link above. `client.ParentLink` is the connection the
    child side makes; `__main__.py` wires it into `mcp.server.lowlevel.
    Server`.

Every actual tool DISPATCH (permission decide -> PreToolUse/PostToolUse
hooks -> the tool's own `run()` -> the session log -> UI events) happens
on the PARENT side (`agent/cc_runtime.py`) -- both halves of this package
are thin, stateless relays with no policy of their own.
"""

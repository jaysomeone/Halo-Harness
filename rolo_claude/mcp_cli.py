"""rolo_claude.mcp_cli -- `rolo-claude mcp ...` subcommand (U0 scope A).
`list` is real now (best-effort: reads the SAME merged config H3's real MCP
client will use -- `~/.claude.json` user + project scope, via
`config/claude_json.py` -- and prints Claude Code's own line SHAPE, just
with an honest "not checked" status since there is no MCP client to health-
check with yet); `add|remove|get|add-json` print the not-yet line (H3 lands
the real MCP client + `mcp add`'s read-modify-write of `~/.claude.json`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rolo_claude.not_yet import print_not_yet

_NOT_YET_SUBCOMMANDS = ("add", "add-from-claude-desktop", "add-json", "get", "login", "logout",
                         "remove", "reset-project-choices", "serve")


def _format_server_line(name: str, spec) -> str:
    status = "not checked (MCP client arrives in a later milestone)"
    if not isinstance(spec, dict):
        return f"{name}: <malformed entry> - {status}"
    stype = spec.get("type", "stdio")
    if stype in ("sse", "http", "streamable-http", "ws", "websocket"):
        url = spec.get("url", "?")
        label = {"sse": "SSE", "http": "HTTP", "streamable-http": "HTTP",
                  "ws": "WebSocket", "websocket": "WebSocket"}.get(stype, stype.upper())
        return f"{name}: {url} ({label}) - {status}"
    command = spec.get("command", "?")
    args = spec.get("args", [])
    args_str = " ".join(str(a) for a in args) if isinstance(args, list) else str(args)
    cmdline = f"{command} {args_str}".strip()
    return f"{name}: {cmdline} - {status}"


def _cmd_list(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude mcp list", add_help=True)
    parser.add_argument("--cwd", default=None, help="Working directory for project-scope server lookup")
    args = parser.parse_args(rest)

    from rolo_claude.config.claude_json import load_claude_json, mcp_servers_for

    cwd = Path(args.cwd) if args.cwd else Path.cwd()
    servers = mcp_servers_for(cwd, load_claude_json())
    if not servers:
        print("No MCP servers configured.")
        return 0
    print("Checking MCP server health...")
    for name in sorted(servers):
        print(_format_server_line(name, servers[name]))
    return 0


def cmd_mcp(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: rolo-claude mcp [options] [command]\n\n"
              "Commands:\n"
              "  list                    List configured MCP servers (real)\n"
              "  get <name>              Get details about an MCP server (not yet)\n"
              "  add [options] <name> <commandOrUrl> [args...]  Add a server (not yet)\n"
              "  add-json <name> <json>  Add a server via JSON (not yet)\n"
              "  remove <name>           Remove a server (not yet)")
        return 0

    sub, rest = argv[0], argv[1:]
    if sub == "list":
        return _cmd_list(rest)
    if sub in _NOT_YET_SUBCOMMANDS:
        print_not_yet(f"mcp {sub}", "H3")
        return 0
    print(f"rolo-claude mcp: unknown subcommand {sub!r}", file=sys.stderr)
    return 2

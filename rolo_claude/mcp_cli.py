"""rolo_claude.mcp_cli -- `rolo-claude mcp ...` subcommand (H3 scope D).
`list`/`get` are real health checks against the SAME resolved config
`rolo_claude.mcp_setup.build_manager` gives a real session (never crash,
always finish within MCP_TIMEOUT); `add`/`add-json`/`remove` are real
read-modify-write edits of `~/.claude.json` (local/user scope) or
`<cwd>/.mcp.json` (project scope) -- `~/.claude.json` is otherwise
NEVER written by this harness, and even these three subcommands only
touch it when rolo explicitly runs one of them, preserving every other
key byte-for-byte (parse -> mutate the one path touched -> re-dump with
the SAME indent width the file already used). Line formats/status
vocabulary match Claude Code's own exactly (binary-facts sec.9).
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Optional

from rolo_claude.config.paths import claude_json_path, normalize_cwd
from rolo_claude.not_yet import print_not_yet

_NOT_YET_SUBCOMMANDS = ("add-from-claude-desktop", "login", "logout", "reset-project-choices", "serve")

_STATUS_LABELS = {
    "connected": "✔ Connected",
    "failed": "✗ Failed to connect",
    "needs_auth": "! Needs authentication",
    "pending_approval": "⏸ Pending approval",
    "disabled": "- Not configured",
    "pending": "- Not configured",
    "connecting": "✗ Connection error",
    "closed": "- Not configured",
    # H13 Part A: a lazy server whose tools came from `mcp.tools_cache`
    # without ever connecting -- rolo-claude's own addition to the status
    # vocabulary (Claude Code has no lazy-start concept, so there is no
    # binary-facts wording to match here).
    "cached": "◐ Cached (connects on first use)",
}


def status_label(entry: dict) -> str:
    """binary-facts sec.9's exact status vocabulary: `✔ Connected`,
    `! Needs authentication`, `! Connected · tools fetch failed`,
    `- Not configured`, `✗ Failed to connect`, `✗ Connection
    error`, `⏸ Pending approval`."""
    if entry.get("state") == "connected" and entry.get("tools_fetch_failed"):
        return "! Connected · tools fetch failed"
    return _STATUS_LABELS.get(entry.get("state"), "✗ Connection error")


def format_mcp_list_line(entry: dict) -> str:
    """`${name}: ${url} (SSE|HTTP) - ${status}` or `${name}: ${command}
    ${args} - ${status}` (binary-facts sec.9, verbatim)."""
    name = entry.get("name", "?")
    label = status_label(entry)
    if entry.get("type") in ("http", "sse"):
        url = entry.get("url") or "?"
        kind = "SSE" if entry.get("type") == "sse" else "HTTP"
        return f"{name}: {url} ({kind}) - {label}"
    command = entry.get("command") or "?"
    args = entry.get("args") or []
    args_str = " ".join(str(a) for a in args)
    cmdline = f"{command} {args_str}".strip()
    return f"{name}: {cmdline} - {label}"


def _session_inputs(cwd: Path):
    from rolo_claude.config.claude_json import is_trusted, load_claude_json
    from rolo_claude.config.settings import resolve_settings
    claude_json = load_claude_json()
    trusted = is_trusted(cwd, claude_json)
    settings = resolve_settings(cwd, trusted=trusted)
    return claude_json, settings


def _cmd_list(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude mcp list", add_help=True)
    parser.add_argument("--cwd", default=None)
    args = parser.parse_args(rest)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    # finding 7: printed BEFORE the health check itself (build_manager's
    # own start=True is what actually connects) -- the old position (after
    # build_manager returned) meant the header always lagged behind the
    # work it's supposed to announce.
    print("Checking MCP server health...")
    try:
        from rolo_claude.mcp_setup import build_manager
        claude_json, settings = _session_inputs(cwd)
        # finding 7: print_mode=False -- an unapproved .mcp.json server
        # must show "⏸ Pending approval" and never actually be spawned just
        # because someone ran `mcp list` in that project (print_mode=True
        # here meant `-p`'s own auto-approve rule leaked into a read-only
        # health check).
        manager, notices = build_manager(cwd=cwd, claude_json=claude_json, settings=settings,
                                          print_mode=False, start=True)
    except Exception as e:  # `mcp list` must NEVER crash -- report and degrade
        print(f"rolo-claude mcp list: could not check server health ({type(e).__name__}: {e})", file=sys.stderr)
        return 1

    for n in notices:
        print(f"Note: {n}", file=sys.stderr)
    if manager is None:
        print("No MCP servers configured.")
        return 0
    try:
        statuses = manager.status()
        if not statuses:
            print("No MCP servers configured.")
            return 0
        for entry in sorted(statuses, key=lambda e: e.get("name", "")):
            print(format_mcp_list_line(entry))
        return 0
    finally:
        manager.close_all()


def _cmd_get(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude mcp get", add_help=True)
    parser.add_argument("name")
    parser.add_argument("--cwd", default=None)
    args = parser.parse_args(rest)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    try:
        from rolo_claude.mcp_setup import build_manager
        claude_json, settings = _session_inputs(cwd)
        # finding 7: same print_mode=False as `mcp list` -- a health check
        # must never auto-approve/spawn an unapproved .mcp.json server.
        manager, notices = build_manager(cwd=cwd, claude_json=claude_json, settings=settings,
                                          print_mode=False, start=True)
    except Exception as e:
        print(f"rolo-claude mcp get: could not check server health ({type(e).__name__}: {e})", file=sys.stderr)
        return 1

    for n in notices:
        print(f"Note: {n}", file=sys.stderr)
    if manager is None or args.name not in manager.handles:
        print(f"No MCP server found with name: {args.name}", file=sys.stderr)
        if manager is not None:
            manager.close_all()
        return 1
    try:
        entry = next(e for e in manager.status() if e.get("name") == args.name)
        print(f"{args.name}:")
        print(f"  Scope: {entry.get('scope')}")
        print(f"  Status: {status_label(entry)}")
        print(f"  Type: {entry.get('type')}")
        if entry.get("type") == "stdio":
            print(f"  Command: {entry.get('command')}")
            print(f"  Args: {' '.join(str(a) for a in (entry.get('args') or []))}")
        else:
            print(f"  URL: {entry.get('url')}")
        if entry.get("instructions"):
            print(f"  Instructions: {entry['instructions']}")
        if entry.get("error"):
            print(f"  Error: {entry['error']}")
        print(f"  Tools: {entry.get('tool_count', 0)}")
        return 0
    finally:
        manager.close_all()


# ---- ~/.claude.json read-modify-write (add / add-json / remove ONLY) ------

def _sniff_indent(text: str) -> int:
    for line in text.splitlines()[1:8]:
        stripped = line.lstrip(" ")
        n = len(line) - len(stripped)
        if n > 0 and stripped != line:
            return n
    return 2


def _read_claude_json_raw() -> "tuple[dict, int, bool, bool]":
    """`(data, indent, had_trailing_newline, had_bom)` -- finding 8:
    `_write_claude_json_raw` needs the SOURCE file's own trailing-newline
    and BOM state to reproduce it exactly, rather than always adding a
    newline the file might never have had and always stripping a BOM it
    might have had. `had_bom` is checked on the raw bytes (`utf-8-sig`
    decoding strips it transparently, so it's otherwise invisible from
    the decoded text alone)."""
    path = claude_json_path()
    if not path.exists():
        return {}, 2, True, False
    raw_bytes = path.read_bytes()
    had_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    text = raw_bytes.decode("utf-8-sig")
    had_trailing_newline = text.endswith("\n")
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data, _sniff_indent(text), had_trailing_newline, had_bom


def _write_claude_json_raw(data: dict, indent: int, *, trailing_newline: bool = True, bom: bool = False) -> None:
    """Read-modify-write: `data` came FROM `_read_claude_json_raw`, so
    every key this process never touched is still exactly as parsed
    (dict insertion order == source JSON order) -- only the one path
    `add`/`add-json`/`remove` mutated actually changes.

    finding 8 fixes, all verified against rolo's real 66837-byte
    `~/.claude.json`: `ensure_ascii=False` (the old `ensure_ascii=True`
    default re-escaped all 40+ non-ASCII characters in that file to
    `\\uXXXX`, starting at the first U+2014); `trailing_newline`/`bom`
    (from `_read_claude_json_raw`) reproduce the source's own state
    instead of always adding a newline / always stripping a BOM; the
    replaced file's permission bits are copied from the ORIGINAL before
    `os.replace` (on Kali, a 0600 file used to become 0644 -- `tmp.write_
    text`'s new file gets the process umask's default, not the source's
    own mode). tmp + `os.replace` for atomicity, same pattern as
    `permissions.add_allow_rule`/`theme.set_config_value`."""
    path = claude_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    if trailing_newline:
        text += "\n"
    raw = text.encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_bytes(raw)
    try:
        if path.exists():
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
    except OSError:
        pass
    os.replace(tmp, path)


def _read_dot_mcp_json(path: Path) -> dict:
    if not path.exists():
        return {"mcpServers": {}}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        data = {}
    data.setdefault("mcpServers", {})
    return data


def _write_dot_mcp_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _build_entry(*, transport: str, command_or_url: str, extra_args: list, env: dict, headers: dict,
                  oauth: Optional[dict]) -> dict:
    if transport == "stdio":
        # H9 MCP-compatibility matrix (item 7, byte-shape parity): the real
        # `claude mcp add` 2.1.281 always writes `"env": {}` for a stdio
        # entry, even with no --env given -- match it exactly so a server
        # added here is byte-identical to one `claude` itself would write.
        entry: dict = {"type": "stdio", "command": command_or_url, "args": list(extra_args), "env": dict(env or {})}
    else:
        entry = {"type": transport, "url": command_or_url}
        if headers:
            entry["headers"] = headers
    if oauth:
        entry["oauth"] = oauth
    return entry


def _parse_kv_list(pairs: list, sep: str) -> dict:
    out = {}
    for item in pairs:
        if sep in item:
            k, _, v = item.partition(sep)
            out[k.strip()] = v.strip()
    return out


def _store_entry(*, scope: str, name: str, entry: dict, cwd: Path) -> str:
    """Writes `entry` into the right place for `scope`; returns a short
    description of where it landed (for the confirmation message)."""
    if scope == "project":
        mcp_json_path = cwd / ".mcp.json"
        data = _read_dot_mcp_json(mcp_json_path)
        data["mcpServers"][name] = entry
        _write_dot_mcp_json(mcp_json_path, data)
        return str(mcp_json_path)

    data, indent, trailing_newline, bom = _read_claude_json_raw()
    if scope == "user":
        data.setdefault("mcpServers", {})
        data["mcpServers"][name] = entry
    else:  # local (default) -- projects[normalize_cwd(cwd)].mcpServers
        data.setdefault("projects", {})
        key = normalize_cwd(cwd)
        proj = data["projects"].setdefault(key, {})
        proj.setdefault("mcpServers", {})
        proj["mcpServers"][name] = entry
    _write_claude_json_raw(data, indent, trailing_newline=trailing_newline, bom=bom)
    return str(claude_json_path())


_ADD_FLAGS_WITH_VALUE = {
    "-t": "transport", "--transport": "transport", "-s": "scope", "--scope": "scope",
    "-e": "env*", "--env": "env*", "-H": "header*", "--header": "header*",
    "--client-id": "client_id", "--client-secret": "client_secret",
    "--callback-port": "callback_port", "--cwd": "cwd",
}


def _parse_add_argv(rest: list) -> "tuple[dict, list]":
    """Manual flag extraction instead of argparse: `-e`/`-H`/etc take a
    value and may appear ANYWHERE before the name, but the trailing
    `<commandOrUrl> [args...]` positionals are very likely to themselves
    start with `-` (`npx -y @playwright/mcp@latest --headless`) --
    argparse's own `nargs="*"` positional refuses to swallow dash-
    prefixed tokens without an explicit `--` separator, so this walks
    `rest` by hand instead. Returns `(opts, positionals)`.

    finding 9 fixes: (a) `mcp add name -- cmd args` (Claude Code's own
    documented form) is now handled -- the FIRST bare `--` token is
    consumed as a plain separator rather than stored as the command
    itself (`{"command": "--", ...}`, a server that could never start);
    a LATER literal `--` (part of the command's own arguments) is left
    alone. (b) `--env=KEY=VALUE` / `--scope=x` inline forms are accepted
    alongside the existing `--env KEY=VALUE` two-token form (still only
    before the first positional, same as every other flag)."""
    opts: dict = {"transport": "stdio", "scope": "local", "env": [], "header": [],
                  "client_id": None, "client_secret": None, "callback_port": None, "cwd": None}
    positionals: list = []
    i = 0
    seen_positionals = False
    dash_dash_consumed = False
    while i < len(rest):
        tok = rest[i]
        if tok == "--" and not dash_dash_consumed:
            dash_dash_consumed = True
            i += 1
            continue
        if not seen_positionals:
            key = _ADD_FLAGS_WITH_VALUE.get(tok)
            if key is None and "=" in tok:
                flag, _, value = tok.partition("=")
                inline_key = _ADD_FLAGS_WITH_VALUE.get(flag)
                if inline_key is not None:
                    if inline_key.endswith("*"):
                        opts[inline_key[:-1]].append(value)
                    else:
                        opts[inline_key] = value
                    i += 1
                    continue
            elif key is not None:
                value = rest[i + 1] if i + 1 < len(rest) else ""
                if key.endswith("*"):
                    opts[key[:-1]].append(value)
                else:
                    opts[key] = value
                i += 2
                continue
        positionals.append(tok)
        seen_positionals = True  # once the FIRST positional (the name) appears, nothing after it is a flag
        i += 1
    return opts, positionals


def _cmd_add(rest: list) -> int:
    if rest and rest[0] in ("-h", "--help"):
        print("Usage: rolo-claude mcp add [-t stdio|sse|http] [-s local|user|project] "
              "[-e KEY=VALUE ...] [-H 'Key: Value' ...] [--cwd DIR] <name> <commandOrUrl> [args...]")
        return 0
    opts, positionals = _parse_add_argv(rest)
    if len(positionals) < 2:
        print("rolo-claude mcp add: requires <name> <commandOrUrl> [args...]", file=sys.stderr)
        return 2
    name, command_or_url, *extra_args = positionals
    if opts["transport"] not in ("stdio", "sse", "http"):
        print(f"rolo-claude mcp add: invalid --transport {opts['transport']!r}", file=sys.stderr)
        return 2
    if opts["scope"] not in ("local", "user", "project"):
        print(f"rolo-claude mcp add: invalid --scope {opts['scope']!r}", file=sys.stderr)
        return 2
    cwd = Path(opts["cwd"]).resolve() if opts["cwd"] else Path.cwd()

    callback_port = int(opts["callback_port"]) if opts["callback_port"] else None
    oauth = {k: v for k, v in (("client_id", opts["client_id"]), ("client_secret", opts["client_secret"]),
                                ("callback_port", callback_port)) if v is not None} or None
    entry = _build_entry(transport=opts["transport"], command_or_url=command_or_url,
                          extra_args=extra_args, env=_parse_kv_list(opts["env"], "="),
                          headers=_parse_kv_list(opts["header"], ":"), oauth=oauth)
    try:
        dest = _store_entry(scope=opts["scope"], name=name, entry=entry, cwd=cwd)
    except (OSError, ValueError) as e:
        print(f"rolo-claude mcp add: {e}", file=sys.stderr)
        return 1
    cmdline = f"{command_or_url} {' '.join(extra_args)}".strip()
    print(f"Added {opts['transport']} MCP server {name!r} ({opts['scope']} scope) to {dest}: {cmdline}")
    return 0


def _cmd_add_json(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude mcp add-json", add_help=True)
    parser.add_argument("-s", "--scope", choices=["local", "user", "project"], default="local")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("name")
    parser.add_argument("json_str")
    args = parser.parse_args(rest)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    try:
        entry = json.loads(args.json_str)
    except ValueError as e:
        print(f"rolo-claude mcp add-json: invalid JSON: {e}", file=sys.stderr)
        return 2
    if not isinstance(entry, dict):
        print("rolo-claude mcp add-json: the JSON value must be an object", file=sys.stderr)
        return 2
    try:
        dest = _store_entry(scope=args.scope, name=args.name, entry=entry, cwd=cwd)
    except (OSError, ValueError) as e:
        print(f"rolo-claude mcp add-json: {e}", file=sys.stderr)
        return 1
    print(f"Added MCP server {args.name!r} ({args.scope} scope) to {dest}")
    return 0


def _cmd_remove(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude mcp remove", add_help=True)
    parser.add_argument("-s", "--scope", choices=["local", "user", "project"], default=None)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("name")
    args = parser.parse_args(rest)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    scopes = [args.scope] if args.scope else ["local", "user", "project"]
    for scope in scopes:
        try:
            if scope == "project":
                mcp_json_path = cwd / ".mcp.json"
                data = _read_dot_mcp_json(mcp_json_path)
                if args.name in data["mcpServers"]:
                    del data["mcpServers"][args.name]
                    _write_dot_mcp_json(mcp_json_path, data)
                    print(f"Removed MCP server {args.name!r} from project scope ({mcp_json_path})")
                    return 0
                continue
            data, indent, trailing_newline, bom = _read_claude_json_raw()
            if scope == "user":
                servers = data.get("mcpServers")
                if isinstance(servers, dict) and args.name in servers:
                    del servers[args.name]
                    _write_claude_json_raw(data, indent, trailing_newline=trailing_newline, bom=bom)
                    print(f"Removed MCP server {args.name!r} from user scope")
                    return 0
            else:  # local
                key = normalize_cwd(cwd)
                projects = data.get("projects")
                if isinstance(projects, dict):
                    for pkey, prec in projects.items():
                        if isinstance(prec, dict) and normalize_cwd(pkey) == key:
                            servers = prec.get("mcpServers")
                            if isinstance(servers, dict) and args.name in servers:
                                del servers[args.name]
                                _write_claude_json_raw(data, indent, trailing_newline=trailing_newline, bom=bom)
                                print(f"Removed MCP server {args.name!r} from local scope")
                                return 0
        except (OSError, ValueError) as e:
            print(f"rolo-claude mcp remove: {e}", file=sys.stderr)
            return 1
    print(f"No MCP server found with name: {args.name}", file=sys.stderr)
    return 1


def cmd_mcp(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: rolo-claude mcp [options] [command]\n\n"
              "Commands:\n"
              "  list                    List configured MCP servers with live health\n"
              "  get <name>              Get details about an MCP server\n"
              "  add [options] <name> <commandOrUrl> [args...]  Add a server\n"
              "  add-json <name> <json>  Add a server via JSON\n"
              "  remove <name>           Remove a server")
        return 0

    sub, rest = argv[0], argv[1:]
    if sub == "list":
        return _cmd_list(rest)
    if sub == "get":
        return _cmd_get(rest)
    if sub == "add":
        return _cmd_add(rest)
    if sub == "add-json":
        return _cmd_add_json(rest)
    if sub == "remove":
        return _cmd_remove(rest)
    if sub in _NOT_YET_SUBCOMMANDS:
        print_not_yet(f"mcp {sub}", "H8")
        return 0
    print(f"rolo-claude mcp: unknown subcommand {sub!r}", file=sys.stderr)
    return 2
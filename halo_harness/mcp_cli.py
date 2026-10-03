"""halo_harness.mcp_cli -- `halo mcp ...` subcommand (H3 scope D).
`list`/`get` are real health checks against the SAME resolved config
`halo_harness.mcp_setup.build_manager` gives a real session (never crash,
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

from halo_harness.config.paths import claude_json_path, normalize_cwd
from halo_harness.not_yet import print_not_yet

# Halo 2.0.1 W4b: every subcommand real `claude mcp` has is now implemented
# (add-from-claude-desktop, login, logout, reset-project-choices, serve) --
# kept as an (empty) tuple rather than deleted so a future genuinely-unbuilt
# one has an obvious place to land, and so cmd_mcp's own fallback below
# still has something to check against.
_NOT_YET_SUBCOMMANDS: "tuple[str, ...]" = ()

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
    # without ever connecting -- halo's own addition to the status
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
    ${args} - ${status}` (binary-facts sec.9, verbatim); W4b connectors
    bridge: `type == "connector"` is halo's own addition (a claude.ai
    account-side connector, discovered through `claude`, has no
    command/url of its own -- see `mcp.connectors_bridge.
    connector_status_entry`) -- labelled `claude.ai connector (via
    claude)` per the gap-list brief, never silently folded into the
    command/url line shape above. A failed/needs-auth entry's `error`
    (when present -- see `mcp.manager`'s own `McpServerHandle.error`) is
    appended so `/mcp`/`mcp list`/doctor show WHY, not just the state."""
    name = entry.get("name", "?")
    label = status_label(entry)
    if entry.get("type") == "connector":
        who = entry.get("connector_name") or name
        where = entry.get("host") or entry.get("url") or "?"
        return f"claude.ai connector (via claude) {who}: {where} - {label}"
    if entry.get("type") in ("http", "sse"):
        url = entry.get("url") or "?"
        kind = "SSE" if entry.get("type") == "sse" else "HTTP"
        line = f"{name}: {url} ({kind}) - {label}"
    else:
        command = entry.get("command") or "?"
        args = entry.get("args") or []
        args_str = " ".join(str(a) for a in args)
        cmdline = f"{command} {args_str}".strip()
        line = f"{name}: {cmdline} - {label}"
    reason = failure_reason(entry)
    return f"{line} ({reason})" if reason else line


def failure_reason(entry: dict) -> str:
    """WHY a `failed`/`needs_auth`/`disabled` server is in that state, for
    `/mcp`, `halo mcp list` and doctor (gap-list brief: "show WHY a server
    failed (command not found on PATH, connection refused on host:port,
    exit code and last stderr line)"); `disabled` (an unsupported/invalid
    transport, including `sdk` or `ws` without SDK support) carries its own
    `disabled_reason` the exact same way (manager.McpServerHandle sets
    `.error` to it) -- same WHY principle, same field. `""` for anything
    else (connected, pending, ...) -- never shown there."""
    if entry.get("state") not in ("failed", "needs_auth", "disabled"):
        return ""
    error = (entry.get("error") or "").strip()
    if not error:
        return ""
    low = error.lower()
    if "filenotfounderror" in low or "no such file or directory" in low or "winerror 2" in low:
        command = entry.get("command") or "?"
        return f"command not found on PATH: {command}"
    if "connectionrefusederror" in low or "connection refused" in low:
        url = entry.get("url") or ""
        from urllib.parse import urlparse
        parsed = urlparse(url) if url else None
        hostport = f"{parsed.hostname}:{parsed.port}" if parsed and parsed.hostname and parsed.port else (url or "?")
        return f"connection refused on {hostport}"
    return error[:200]


def _session_inputs(cwd: Path):
    from halo_harness.config.claude_json import is_trusted, load_claude_json
    from halo_harness.config.settings import resolve_settings
    claude_json = load_claude_json()
    trusted = is_trusted(cwd, claude_json)
    settings = resolve_settings(cwd, trusted=trusted)
    return claude_json, settings


def _cmd_list(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="halo mcp list", add_help=True)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--refresh", action="store_true",
                         help="force a fresh claude.ai connectors discovery, ignoring any cache")
    args = parser.parse_args(rest)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    # finding 7: printed BEFORE anything that could fail (build_manager's
    # own start=True is what actually connects) -- the old position (after
    # build_manager returned) meant the header always lagged behind the
    # work it's supposed to announce. Stays the very FIRST line
    # unconditionally (pinned by
    # test_mcp_list_prints_checking_header_before_server_lines).
    print("Checking MCP server health...")
    try:
        claude_json, settings = _session_inputs(cwd)
    except Exception as e:  # `mcp list` must NEVER crash -- report and degrade
        print(f"halo mcp list: could not resolve settings ({type(e).__name__}: {e})", file=sys.stderr)
        return 1

    # "explain the zero" (gap-list brief, W4b item 1): what was searched
    # (every scope, with its own count) and what claude.ai connectors
    # `claude` reports -- so a truly empty result is never a bare "No MCP
    # servers configured." with no context. Never allowed to crash `mcp
    # list` itself (same contract as the health check below).
    try:
        from halo_harness.mcp import connectors_bridge
        if args.refresh:
            connectors_bridge.refresh_now()
        else:
            # W5 ("connector cold start"): without --refresh, still do ONE
            # bounded synchronous discovery when the cache is genuinely
            # empty (a no-op otherwise -- see the function's own gating),
            # so a fresh box's first `halo mcp list` shows the real
            # connectors instead of needing --refresh once first. Eligibility
            # reads the cached claude.ai login, and a fresh process has no
            # cache yet, so prime it first the way `halo providers` does
            # (gateway-driven `claude` is never spawned by that refresh).
            # W5b: this prime-then-discover pair is now the shared helper
            # `headless.build_session`'s own print-mode cold-start path uses
            # too (`connectors_bridge.prime_auth_cache_if_stale`).
            connectors_bridge.prime_auth_cache_if_stale()
            connectors_bridge.ensure_discovered_synchronously_if_cold()
        from halo_harness.mcp import explain
        for line in explain.explain_lines(cwd=cwd, claude_json=claude_json, settings=settings):
            print(line)
    except Exception as e:
        print(f"Note: could not explain MCP scopes/connectors ({type(e).__name__}: {e})", file=sys.stderr)

    try:
        from halo_harness.mcp_setup import build_manager
        # finding 7: print_mode=False -- an unapproved .mcp.json server
        # must show "⏸ Pending approval" and never actually be spawned just
        # because someone ran `mcp list` in that project (print_mode=True
        # here meant `-p`'s own auto-approve rule leaked into a read-only
        # health check).
        manager, notices = build_manager(cwd=cwd, claude_json=claude_json, settings=settings,
                                          print_mode=False, start=True)
    except Exception as e:  # `mcp list` must NEVER crash -- report and degrade
        print(f"halo mcp list: could not check server health ({type(e).__name__}: {e})", file=sys.stderr)
        return 1

    for n in notices:
        print(f"Note: {n}", file=sys.stderr)
    if manager is None:
        print("No locally-configured MCP servers (see above for connectors/other scopes).")
        return 0
    try:
        statuses = manager.status()
        if not statuses:
            print("No MCP servers configured in this directory (see above for connectors/other scopes).")
            return 0
        for entry in sorted(statuses, key=lambda e: e.get("name", "")):
            print(format_mcp_list_line(entry))
        return 0
    finally:
        manager.close_all()


def _cmd_get(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="halo mcp get", add_help=True)
    parser.add_argument("name")
    parser.add_argument("--cwd", default=None)
    args = parser.parse_args(rest)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    try:
        from halo_harness.mcp_setup import build_manager
        claude_json, settings = _session_inputs(cwd)
        # finding 7: same print_mode=False as `mcp list` -- a health check
        # must never auto-approve/spawn an unapproved .mcp.json server.
        manager, notices = build_manager(cwd=cwd, claude_json=claude_json, settings=settings,
                                          print_mode=False, start=True)
    except Exception as e:
        print(f"halo mcp get: could not check server health ({type(e).__name__}: {e})", file=sys.stderr)
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
        print("Usage: halo mcp add [-t stdio|sse|http] [-s local|user|project] "
              "[-e KEY=VALUE ...] [-H 'Key: Value' ...] [--cwd DIR] <name> <commandOrUrl> [args...]")
        return 0
    opts, positionals = _parse_add_argv(rest)
    if len(positionals) < 2:
        print("halo mcp add: requires <name> <commandOrUrl> [args...]", file=sys.stderr)
        return 2
    name, command_or_url, *extra_args = positionals
    if opts["transport"] not in ("stdio", "sse", "http"):
        print(f"halo mcp add: invalid --transport {opts['transport']!r}", file=sys.stderr)
        return 2
    if opts["scope"] not in ("local", "user", "project"):
        print(f"halo mcp add: invalid --scope {opts['scope']!r}", file=sys.stderr)
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
        print(f"halo mcp add: {e}", file=sys.stderr)
        return 1
    cmdline = f"{command_or_url} {' '.join(extra_args)}".strip()
    print(f"Added {opts['transport']} MCP server {name!r} ({opts['scope']} scope) to {dest}: {cmdline}")
    return 0


def _cmd_add_json(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="halo mcp add-json", add_help=True)
    parser.add_argument("-s", "--scope", choices=["local", "user", "project"], default="local")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("name")
    parser.add_argument("json_str")
    args = parser.parse_args(rest)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    try:
        entry = json.loads(args.json_str)
    except ValueError as e:
        print(f"halo mcp add-json: invalid JSON: {e}", file=sys.stderr)
        return 2
    if not isinstance(entry, dict):
        print("halo mcp add-json: the JSON value must be an object", file=sys.stderr)
        return 2
    try:
        dest = _store_entry(scope=args.scope, name=args.name, entry=entry, cwd=cwd)
    except (OSError, ValueError) as e:
        print(f"halo mcp add-json: {e}", file=sys.stderr)
        return 1
    print(f"Added MCP server {args.name!r} ({args.scope} scope) to {dest}")
    return 0


def _cmd_remove(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="halo mcp remove", add_help=True)
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
            print(f"halo mcp remove: {e}", file=sys.stderr)
            return 1
    print(f"No MCP server found with name: {args.name}", file=sys.stderr)
    return 1


def _wsl_windows_appdata() -> Optional[Path]:
    """Best-effort: the Windows side's `%APPDATA%` from inside WSL, via
    `/mnt/c/Users/<name>/AppData/Roaming` -- tries `cmd.exe`'s own
    `%USERNAME%` first, then every real user directory under
    `/mnt/c/Users` that actually has one."""
    users_dir = Path("/mnt/c/Users")
    if not users_dir.is_dir():
        return None
    username = ""
    try:
        import subprocess
        proc = subprocess.run(["cmd.exe", "/c", "echo %USERNAME%"], capture_output=True, text=True, timeout=5)
        username = proc.stdout.strip()
    except Exception:
        pass
    skip = {"public", "default", "default user", "all users"}
    candidates = ([users_dir / username] if username else []) + sorted(
        p for p in users_dir.iterdir() if p.is_dir() and p.name.lower() not in skip)
    for cand in candidates:
        appdata = cand / "AppData" / "Roaming"
        if appdata.is_dir():
            return appdata
    return None


def claude_desktop_config_path() -> "tuple[Optional[Path], Optional[str]]":
    """`(path, reason_if_unsupported)` -- Claude Desktop itself only ships
    for macOS and Windows (real `claude mcp add-from-claude-desktop`'s own
    help text: "Mac and WSL only", WSL being how its Linux binary reaches
    the Windows side); halo runs natively on Windows too, so that's
    supported directly here, not just through WSL.

    Test seam: `BRIDGE_TEST_CLAUDE_DESKTOP_CONFIG` overrides the resolved
    path outright (never touches a real `%APPDATA%`/`~/Library` on the
    machine running the test)."""
    override = os.environ.get("BRIDGE_TEST_CLAUDE_DESKTOP_CONFIG")
    if override is not None:
        return Path(override), None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json", None
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None, "%APPDATA% is not set"
        return Path(appdata) / "Claude" / "claude_desktop_config.json", None
    try:
        is_wsl = "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        is_wsl = False
    if not is_wsl:
        return None, "Claude Desktop ships for Mac and Windows only (WSL can reach the Windows side) -- nothing to import on this platform"
    win_appdata = _wsl_windows_appdata()
    if win_appdata is None:
        return None, "could not find the Windows user's AppData under /mnt/c from WSL"
    return win_appdata / "Claude" / "claude_desktop_config.json", None


def _cmd_add_from_claude_desktop(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="halo mcp add-from-claude-desktop", add_help=True)
    parser.add_argument("-s", "--scope", choices=["local", "user", "project"], default="local")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--dry-run", action="store_true", help="list what would be imported, write nothing")
    args = parser.parse_args(rest)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    path, reason = claude_desktop_config_path()
    if path is None:
        print(f"halo mcp add-from-claude-desktop: {reason}", file=sys.stderr)
        return 1
    if not path.exists():
        print(f"halo mcp add-from-claude-desktop: no Claude Desktop config found at {path}", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        print(f"halo mcp add-from-claude-desktop: could not read {path}: {e}", file=sys.stderr)
        return 1
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or not servers:
        print(f"halo mcp add-from-claude-desktop: no mcpServers found in {path}")
        return 0

    imported = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        if not args.dry_run:
            try:
                _store_entry(scope=args.scope, name=name, entry=entry, cwd=cwd)
            except (OSError, ValueError) as e:
                print(f"halo mcp add-from-claude-desktop: {name}: {e}", file=sys.stderr)
                continue
        imported.append(name)
    verb = "Would import" if args.dry_run else "Imported"
    names = ", ".join(sorted(imported)) or "(none)"
    print(f"{verb} {len(imported)} server(s) from {path} ({args.scope} scope): {names}")
    return 0


def _cmd_reset_project_choices(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="halo mcp reset-project-choices", add_help=True)
    parser.add_argument("--cwd", default=None)
    args = parser.parse_args(rest)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    from halo_harness.mcp_setup import reset_project_approvals
    n = reset_project_approvals(cwd / ".mcp.json")
    print(f"Reset {n} approved project-scoped (.mcp.json) server choice(s) in {cwd} -- "
          f"halo will ask again the next time each one is used.")
    return 0


def _cmd_serve(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="halo mcp serve", add_help=True)
    parser.add_argument("--cwd", default=None)
    args = parser.parse_args(rest)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    from halo_harness.mcp import NOT_AVAILABLE_NOTICE, available
    if not available():
        print(f"halo mcp serve: {NOT_AVAILABLE_NOTICE}", file=sys.stderr)
        return 1
    import asyncio
    from halo_harness.mcp.serve import run_server
    try:
        asyncio.run(run_server(cwd))
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_login(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="halo mcp login", add_help=True)
    parser.add_argument("name")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--no-browser", action="store_true", help="print the URL only, never auto-open a browser")
    parser.add_argument("--timeout", type=float, default=None, help="seconds to wait for the browser redirect")
    args = parser.parse_args(rest)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    from halo_harness.config.claude_json import load_claude_json
    from halo_harness.mcp.manager import resolve_server_configs
    resolved, _notices = resolve_server_configs(cwd=cwd, claude_json=load_claude_json())
    cfg = resolved.get(args.name)
    if cfg is None:
        print(f"halo mcp login: no MCP server found with name: {args.name}", file=sys.stderr)
        return 1
    if cfg.type not in ("http", "sse"):
        print(f"halo mcp login: {args.name!r} is a {cfg.type!r} server -- OAuth only applies to "
              f"http/sse (remote) servers.", file=sys.stderr)
        return 1

    from halo_harness.mcp import oauth
    kwargs = {"callback_timeout": args.timeout} if args.timeout is not None else {}
    tokens, err = oauth.run_authorization_flow(server_name=args.name, server_url=cfg.url or "",
                                                oauth_cfg=cfg.oauth or {}, open_browser=not args.no_browser, **kwargs)
    if err:
        print(f"halo mcp login: {args.name}: {err}", file=sys.stderr)
        return 1
    path = oauth.save_tokens(args.name, tokens)
    print(f"halo mcp login: {args.name}: authorized, tokens stored at {path}")
    return 0


def _cmd_logout(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="halo mcp logout", add_help=True)
    parser.add_argument("name")
    args = parser.parse_args(rest)
    from halo_harness.mcp import oauth
    if oauth.clear_tokens(args.name):
        print(f"halo mcp logout: {args.name}: cleared stored OAuth credentials.")
    else:
        print(f"halo mcp logout: {args.name}: no stored OAuth credentials to clear.")
    return 0


def cmd_mcp(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: halo mcp [options] [command]\n\n"
              "Commands:\n"
              "  list                    List configured MCP servers with live health\n"
              "  get <name>              Get details about an MCP server\n"
              "  add [options] <name> <commandOrUrl> [args...]  Add a server\n"
              "  add-json <name> <json>  Add a server via JSON\n"
              "  remove <name>           Remove a server\n"
              "  add-from-claude-desktop Import MCP servers from Claude Desktop's config\n"
              "  reset-project-choices   Reset approved project-scoped (.mcp.json) servers\n"
              "  serve                   Expose halo's own built-in tools as an MCP server\n"
              "  login <name>            OAuth-authenticate a remote MCP server\n"
              "  logout <name>           Clear stored OAuth credentials for a server")
        return 0

    sub, rest = argv[0], argv[1:]
    if sub == "list":
        return _cmd_list(rest)
    if sub == "get":
        return _cmd_get(rest)
    if sub == "add":
        return _cmd_add(rest)
    if sub == "add-from-claude-desktop":
        return _cmd_add_from_claude_desktop(rest)
    if sub == "reset-project-choices":
        return _cmd_reset_project_choices(rest)
    if sub == "serve":
        return _cmd_serve(rest)
    if sub == "login":
        return _cmd_login(rest)
    if sub == "logout":
        return _cmd_logout(rest)
    if sub == "add-json":
        return _cmd_add_json(rest)
    if sub == "remove":
        return _cmd_remove(rest)
    if sub in _NOT_YET_SUBCOMMANDS:
        print_not_yet(f"mcp {sub}", "H8")
        return 0
    print(f"halo mcp: unknown subcommand {sub!r}", file=sys.stderr)
    return 2
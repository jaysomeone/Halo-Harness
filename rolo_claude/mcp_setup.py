"""rolo_claude.mcp_setup -- shared MCP manager construction (H3). Used by
BOTH `headless.py` (building a real session's tools) and `mcp_cli.py`'s
`mcp list` (a health check against the EXACT SAME resolved config) so the
two can never disagree about which servers exist or how scope resolution/
approval/`${VAR}` expansion works. Also builds the `--chrome`/`--playwright`
dynamic server entries (H3 scope E, brought forward from H7 per the brief:
"just two dynamic MCP entries").
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from rolo_claude.config.paths import bridge_home, home, managed_dir


def load_mcp_approvals() -> dict:
    """`~/.rolo-claude/mcp-approvals.json` -- OUR OWN approval store for a
    `.mcp.json` project server (D-CFG: "our `~/.rolo-claude/mcp-approvals.
    json`"), never Claude Code's own state. Must-do: keys are each
    approved entry's `manager.mcp_approval_key(raw_entry)` sha256 (an
    edited/tampered entry needs re-approval), never the bare server name.
    `{}` if missing/invalid."""
    path = bridge_home() / "mcp-approvals.json"
    try:
        import json
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def record_mcp_approval(name: str, raw_entry: dict) -> None:
    """Persist an approval for one `.mcp.json` server entry, read-modify-
    write, best-effort (never raises -- an approval that fails to save
    just means the user gets asked again next time, not a crashed
    session). Ready for U2's interactive `question`-event approval flow
    (loop.py's Session.run()/permission-wait-seam, not this module's job)
    to call once the user answers "yes"."""
    from rolo_claude.mcp.manager import mcp_approval_key
    path = bridge_home() / "mcp-approvals.json"
    data = load_mcp_approvals()
    data[mcp_approval_key(raw_entry)] = {"name": name}
    try:
        import json
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def managed_mcp_json_path() -> Path:
    return managed_dir() / "managed-mcp.json"


def find_claude_exe() -> Optional[str]:
    """OS-neutral `claude` binary lookup for `--chrome` (Kali is this
    project's PRIMARY target -- `bridge.py`'s own `find_claude_exe` looks
    ONLY for `claude.exe`/`claude.cmd`, so it's never reused here;
    duplicating the small lookup keeps this module usable on Linux without
    touching the proxy's own Windows-only helper)."""
    env_exe = os.environ.get("BRIDGE_CLAUDE_EXE")
    if env_exe:
        return env_exe
    for name in ("claude", "claude.exe", "claude.cmd"):
        found = shutil.which(name)
        if found:
            return found
    for rel in ("claude", "claude.exe"):
        candidate = home() / ".local" / "bin" / rel
        if candidate.exists():
            return str(candidate)
    return None


def _env_bool(name: str) -> Optional[bool]:
    """`None` when unset; else the var's own boolean reading ("0"/"false"/
    "no"/"off"/"" -> False, anything else -> True)."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def resolve_chrome_enabled(claude_json: dict, *, chrome_flag: bool, no_chrome_flag: bool,
                            interactive: bool = True) -> bool:
    """finding 14: the binary's own enable order -- flag -> env var ->
    off for non-interactive sessions -> `claudeInChromeDefaultEnabled`.
    `--no-chrome` always wins outright first (not itself part of the
    documented ladder, but the one override nothing below it can undo).
    `interactive=False` (headless.py's own `-p` call site) means
    `claudeInChromeDefaultEnabled` is NEVER consulted -- before this,
    every `rolo-claude -p` on a box with that setting on spawned
    `claude.CMD --claude-in-chrome-mcp`, which `claude -p` itself never
    does. The TUI (interactive, U2's own call site) keeps the default
    `interactive=True` and is unaffected."""
    if no_chrome_flag:
        return False
    if chrome_flag:
        return True
    env_val = _env_bool("CLAUDE_CODE_ENABLE_CFC")
    if env_val is not None:
        return env_val
    if not interactive:
        return False
    return bool((claude_json or {}).get("claudeInChromeDefaultEnabled"))


def chrome_server_config(*, bypass_mode: bool):
    """`(config_or_None, error_or_None)`. `{type: stdio, command: <claude
    exe>, args: ["--claude-in-chrome-mcp"]}`, named `claude-in-chrome`
    [verified doc]; `CLAUDE_CHROME_PERMISSION_MODE=skip_all_permission_
    checks` only in auto/bypass modes (matches Claude Code's own bypass
    behaviour)."""
    from rolo_claude.mcp.manager import McpServerConfig
    claude_exe = find_claude_exe()
    if not claude_exe:
        return None, ("--chrome: no claude executable found on PATH -- Claude in Chrome needs Claude "
                       "Code's own binary to spawn the claude-in-chrome MCP server (see `doctor`).")
    env = {"CLAUDE_CHROME_PERMISSION_MODE": "skip_all_permission_checks"} if bypass_mode else {}
    return McpServerConfig(name="claude-in-chrome", type="stdio", command=claude_exe,
                            args=["--claude-in-chrome-mcp"], env=env, scope="dynamic"), None


def playwright_server_config(*, cdp_endpoint: Optional[str] = None, headless: bool = False):
    """`(config_or_None, error_or_None)`. `{type: stdio, command: npx,
    args: ["-y", "@playwright/mcp@latest", ...]}`, named `playwright`;
    `--playwright-cdp <endpoint>` -> `--cdp-endpoint <endpoint>`,
    `--playwright-headless` -> `--headless` passthrough to the server."""
    from rolo_claude.mcp.manager import McpServerConfig
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        return None, ("--playwright: npx not found on PATH -- @playwright/mcp needs node/npx "
                       "(see `doctor`).")
    args = ["-y", "@playwright/mcp@latest"]
    if cdp_endpoint:
        args += ["--cdp-endpoint", cdp_endpoint]
    if headless:
        args += ["--headless"]
    return McpServerConfig(name="playwright", type="stdio", command=npx, args=args, env={}, scope="dynamic"), None


def build_manager(
    *, cwd: Path, claude_json: dict, settings=None, print_mode: bool = True,
    mcp_config_flag: Optional[list] = None, strict_mcp_config: bool = False,
    chrome: bool = False, playwright: bool = False,
    playwright_cdp: Optional[str] = None, playwright_headless: bool = False,
    bypass_mode: bool = False, start: bool = True, trusted: bool = True,
):
    """`(manager_or_None, notices)`. `manager` is None ONLY when the `mcp`
    package itself isn't installed (`rolo_claude.mcp.available()` False)
    -- `notices` then carries exactly `NOT_AVAILABLE_NOTICE`. A real but
    EMPTY server set (no servers configured at all) still returns a real
    (zero-handle) `McpManager`, never None, so a caller never has to tell
    "not installed" apart from "installed, nothing configured" by hand.
    `start=False` (mcp_cli.py's `mcp get`/`mcp add` etc.) resolves configs
    without connecting to anything. `trusted` (default True, so an
    existing caller that doesn't pass it keeps today's behaviour) gates a
    PROJECT-scope `.mcp.json` server's `headersHelper` (binary-facts sec.9:
    "repo-resident config needs persisted trust")."""
    from rolo_claude.mcp import NOT_AVAILABLE_NOTICE, available
    notices: list = []
    if not available():
        return None, [NOT_AVAILABLE_NOTICE]

    from rolo_claude.mcp.manager import McpManager, resolve_server_configs
    from rolo_claude.providers.config import tool_child_env

    dynamic: dict = {}
    if chrome:
        cfg, err = chrome_server_config(bypass_mode=bypass_mode)
        if cfg is not None:
            dynamic["claude-in-chrome"] = cfg
        elif err:
            notices.append(err)
    if playwright:
        cfg, err = playwright_server_config(cdp_endpoint=playwright_cdp, headless=playwright_headless)
        if cfg is not None:
            dynamic["playwright"] = cfg
        elif err:
            notices.append(err)

    base_env = settings.effective_env if settings is not None else dict(os.environ)

    from rolo_claude.config.plugins import discover_plugin_mcp_servers
    plugin_servers, plugin_notices = discover_plugin_mcp_servers(env=base_env, settings=settings)
    notices.extend(plugin_notices)

    configs, resolve_notices = resolve_server_configs(
        cwd=cwd, claude_json=claude_json, mcp_config_flag=mcp_config_flag,
        strict_mcp_config=strict_mcp_config, print_mode=print_mode,
        approvals=load_mcp_approvals(), managed_mcp_path=managed_mcp_json_path(),
        env_for_expansion=base_env, extra_dynamic=dynamic, settings=settings,
        plugin_servers=plugin_servers,
    )
    notices.extend(resolve_notices)

    # finding 13 must-do: server-level "mcpLazy" -- connect on first tool
    # use instead of at start_all() time (parsed into McpServerConfig.lazy
    # but never consulted anywhere until now).
    lazy_names = {name for name, cfg in configs.items() if cfg.lazy}
    manager = McpManager(configs, tool_env=tool_child_env(base_env), cwd=cwd,
                          lazy_names=lazy_names, trusted=trusted)
    if start:
        manager.start_all()
    return manager, notices

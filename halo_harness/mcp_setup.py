"""halo_harness.mcp_setup -- shared MCP manager construction (H3). Used by
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

from halo_harness.config.paths import bridge_home, home, managed_dir


def load_mcp_approvals() -> dict:
    """`~/.halo/mcp-approvals.json` -- OUR OWN approval store for a
    `.mcp.json` project server (D-CFG: "our `~/.halo/mcp-approvals.
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
    from halo_harness.mcp.manager import mcp_approval_key
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


def reset_project_approvals(mcp_json_path: Path) -> int:
    """`halo mcp reset-project-choices` (Halo 2.0.1 gap-list brief): forgets
    every approval recorded for an entry CURRENTLY present in this
    project's `.mcp.json` -- real `claude mcp reset-project-choices`'s own
    wording: "Reset all approved and rejected project-scoped (.mcp.json)
    servers within this project." Halo's own approval store only ever
    tracks APPROVALS (a server simply stays `pending_approval` until
    approved -- there is no separate persisted "rejected" state to also
    reset). An entry approved under a PRIOR version of a since-edited
    server (a different `mcp_approval_key`) is already unreachable from
    `.mcp.json`'s CURRENT content and needs no reset -- it already demands
    re-approval. Returns the count actually forgotten; never raises."""
    if not mcp_json_path.exists():
        return 0
    try:
        import json
        data = json.loads(mcp_json_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return 0
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return 0
    from halo_harness.mcp.manager import mcp_approval_key
    keys = {mcp_approval_key(raw) for raw in servers.values() if isinstance(raw, dict)}
    approvals = load_mcp_approvals()
    removed = [k for k in keys if k in approvals]
    if not removed:
        return 0
    for k in removed:
        approvals.pop(k, None)
    try:
        import json
        path = bridge_home() / "mcp-approvals.json"
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(approvals, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return 0
    return len(removed)


def managed_mcp_json_path() -> Path:
    return managed_dir() / "managed-mcp.json"


def find_claude_exe() -> Optional[str]:
    """OS-neutral `claude` binary lookup for `--chrome` (Kali is this
    project's PRIMARY target -- `bridge.py`'s own `find_claude_exe` looks
    ONLY for `claude.exe`/`claude.cmd`, so it's never reused here;
    duplicating the small lookup keeps this module usable on Linux without
    touching the proxy's own Windows-only helper)."""
    from halo_harness.config.paths import env_compat
    env_exe = env_compat("CLAUDE_EXE")
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
    every `halo -p` on a box with that setting on spawned
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


def chrome_server_config(*, bypass_mode: bool = True):
    """`(config_or_None, error_or_None)`. `{type: stdio, command: <claude
    exe>, args: ["--claude-in-chrome-mcp"]}`, named `claude-in-chrome`
    [verified doc].

    finding 9 (major, h4-h5-h3c review) / "Auto mode = uninterrupted"
    (plan, binding): `CLAUDE_CHROME_PERMISSION_MODE=skip_all_permission_
    checks` is now ALWAYS set, unconditionally -- halo's own
    PermissionEngine already gates every `mcp__claude-in-chrome__*` tool
    call the SAME way it gates any other tool (auto/bypass allow outright;
    default/acceptEdits/plan honour the user's own ask/deny rules), so the
    extension's OWN separate internal prompt is pure double-gating, never
    a second layer of real protection. Worse, the pre-fix version baked
    the flag in at SPAWN time from whatever mode the session happened to
    be in THEN: a session started in `default` and switched to `auto` via
    Shift+Tab mid-session kept getting the extension's own permission
    prompt on every browser action for the rest of the process (the MCP
    server subprocess, once spawned, never sees a LATER mode change) --
    exactly the kind of mode-shaped tool restriction the plan's "Auto mode
    = uninterrupted + steering" section forbids outright. `bypass_mode` is
    kept as a parameter (unused) only so existing callers that still pass
    it keep working unchanged."""
    from halo_harness.mcp.manager import McpServerConfig
    claude_exe = find_claude_exe()
    if not claude_exe:
        return None, ("--chrome: no claude executable found on PATH -- Claude in Chrome needs Claude "
                       "Code's own binary to spawn the claude-in-chrome MCP server (see `doctor`).")
    env = {"CLAUDE_CHROME_PERMISSION_MODE": "skip_all_permission_checks"}
    return McpServerConfig(name="claude-in-chrome", type="stdio", command=claude_exe,
                            args=["--claude-in-chrome-mcp"], env=env, scope="dynamic"), None


def playwright_server_config(*, cdp_endpoint: Optional[str] = None, headless: bool = False):
    """`(config_or_None, error_or_None)`. `{type: stdio, command: npx,
    args: ["-y", "@playwright/mcp@latest", ...]}`, named `playwright`;
    `--playwright-cdp <endpoint>` -> `--cdp-endpoint <endpoint>`,
    `--playwright-headless` -> `--headless` passthrough to the server."""
    from halo_harness.mcp.manager import McpServerConfig
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    node = shutil.which("node") or shutil.which("node.exe")
    if not npx or not node:
        # H9 Linux acceptance (Part A, bug 4): fail FAST and honestly, the
        # way --chrome does, whenever EITHER half is missing -- `doctor`
        # already checks both node and npx, but this only checked npx, so
        # a box with an `npx` shim and no runnable `node` (WSL's Windows-
        # side npx through PE interop was the observed case) built a
        # server config that then hung/misbehaved for the whole session.
        missing = ", ".join(n for n, p in (("node", node), ("npx", npx)) if not p)
        return None, (f"--playwright: {missing} not found on PATH -- @playwright/mcp needs node/npx "
                       f"(see `doctor`).")
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
    extra_plugin_roots: Optional[list] = None,
):
    """`(manager_or_None, notices)`. `manager` is None ONLY when the `mcp`
    package itself isn't installed (`halo_harness.mcp.available()` False)
    -- `notices` then carries exactly `NOT_AVAILABLE_NOTICE`. A real but
    EMPTY server set (no servers configured at all) still returns a real
    (zero-handle) `McpManager`, never None, so a caller never has to tell
    "not installed" apart from "installed, nothing configured" by hand.
    `start=False` (mcp_cli.py's `mcp get`/`mcp add` etc.) resolves configs
    without connecting to anything. `trusted` (default True, so an
    existing caller that doesn't pass it keeps today's behaviour) gates a
    PROJECT-scope `.mcp.json` server's `headersHelper` (binary-facts sec.9:
    "repo-resident config needs persisted trust")."""
    from halo_harness.mcp import NOT_AVAILABLE_NOTICE, available
    notices: list = []
    if not available():
        return None, [NOT_AVAILABLE_NOTICE]

    from halo_harness.mcp.manager import McpManager, resolve_server_configs
    from halo_harness.providers.config import tool_child_env

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

    from halo_harness.config.plugins import discover_plugin_mcp_servers
    # finding 8: `cwd` threaded through so a V2 project/local-scoped
    # plugin record (`projectPath`) is matched against THIS session's own
    # working directory, not silently dropped. `extra_plugin_roots` (W5,
    # carried from W4a) is `--plugin-dir`/`--plugin-url`'s own resolved
    # directories -- merged in exactly like an installed plugin's servers.
    plugin_servers, plugin_notices = discover_plugin_mcp_servers(
        env=base_env, settings=settings, cwd=cwd, extra_roots=extra_plugin_roots,
    )
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
    # but never consulted anywhere until now). H13 Part A: `cfg.lazy` is now
    # ALREADY the fully-resolved effective value (per-server override, else
    # the global `settings.json` "mcpLazy" default, else True) --
    # `resolve_server_configs`/`_apply_lazy_defaults` did that resolution;
    # this stays the same one-line set comprehension it always was.
    lazy_names = {name for name, cfg in configs.items() if cfg.lazy}
    manager = McpManager(configs, tool_env=tool_child_env(base_env), cwd=cwd,
                          lazy_names=lazy_names, trusted=trusted)
    if start:
        manager.start_all()
        if lazy_names:
            notices.extend(bootstrap_lazy_from_cache(manager, configs, lazy_names))
    return manager, notices


def bootstrap_lazy_from_cache(manager, configs: "dict[str, object]", lazy_names: set) -> list:
    """H13 Part A: every lazy server needs its tool NAMES/DESCRIPTIONS known
    before the frozen catalog is built, even though it must not actually
    connect yet. For each lazy server: a cache file whose hash matches this
    server's CURRENT config (`mcp.tools_cache.config_cache_key`) seeds the
    handle straight from disk (`McpManager.mark_cached` -- zero connections);
    no cache, or a hash mismatch (first session ever, or the command/args/
    env/url/headers changed since it was last cached) means it needs a real
    connect to learn its tools for the first time.

    Bug found live (H13 Part D dogfooding, before any real acceptance line
    was recorded): this used to `h.start()` -- a BLOCKING call -- ONE
    uncached server at a time in a plain `for` loop. A box with N lazy
    servers and no cache yet (every real box's very FIRST run, or right
    after any of their configs change) paid up to N * MCP_TIMEOUT (30s
    default) SEQUENTIALLY -- observed hanging past 400s against a real
    15-server `~/.claude.json` with a few unreachable/hardware-dependent
    entries, worse than the pre-H13 eager `start_all()` (which at least
    already ran every eager server concurrently via `_start_targets_
    parallel`). Every uncached server now connects together via
    `McpManager.start_many` (the SAME concurrent-gather machinery
    `start_all()`/`ensure_lazy_started_all()` already use), one shared
    MCP_TIMEOUT window for the whole batch, not one per server -- pinned by
    `tests/test_mcp_lazy_cache.py::
    test_bootstrap_connects_multiple_uncached_lazy_servers_in_parallel_not_serially`.

    A connect failure is left exactly as `McpManager.status()` already
    reports it (`failed`/`needs_auth`) -- no different from an eager server
    failing at `start_all()` time; nothing here fabricates or swallows
    that. Returns any notices worth surfacing (currently none on the happy
    path -- kept as a list return, matching every other notices-returning
    function in this module, for whichever future case needs one)."""
    from halo_harness.mcp import tools_cache
    notices: list = []
    needs_connect: list = []
    for name in lazy_names:
        h = manager.handles.get(name)
        cfg = configs.get(name)
        if h is None or cfg is None or h.state != "pending":
            continue  # disabled/pending_approval -- nothing this bootstrap can do
        key = tools_cache.config_cache_key(cfg)
        entry = tools_cache.read_cache(name)
        if entry is not None and entry.get("hash") == key:
            manager.mark_cached(name, [tools_cache.tool_from_dict(d) for d in (entry.get("tools") or [])],
                                 entry.get("instructions"))
        else:
            needs_connect.append(name)

    if needs_connect:
        manager.start_many(needs_connect)
        for name in needs_connect:
            h = manager.handles[name]
            if h.state == "connected":
                tools_cache.write_cache(name, key=tools_cache.config_cache_key(configs[name]),
                                         tools=h.tools, instructions=h.instructions)
    return notices

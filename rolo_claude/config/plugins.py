"""rolo_claude.config.plugins -- discovery of plugin-provided MCP servers
under ~/.claude/plugins/ (H3b closing requirement: "every server a user
adds to Claude Code must work"). Installed Claude Code PLUGINS can bundle
their own MCP servers, same as a user's own .mcp.json/~/.claude.json
entries -- none of that was wired into this harness before this module.

rolo has zero plugins installed as of 2026-09-24, so this is built and
verified against a FIXTURE plugin (tests/helpers/fake_home.py::
add_fake_plugin), not a real one. The on-disk layout below is this
harness's own best-effort mirror of Claude Code's real one (binary-facts
sec.9 only documents the `CLAUDE_PLUGIN_ROOT` env var itself, not the
layout it comes from) -- documented here for whoever next compares it
against a real install:

  <configDir>/plugins/installed_plugins.json
      {"plugins": {"<plugin-name>": {"enabled": bool, "path"?: str}}}
      -- "path", when present, points straight at the plugin's root (an
      explicit --plugin-dir-style install); otherwise the plugin's files
      are looked up under the cache dir below, by name. A plugin missing
      `"enabled"` counts as enabled (installed == on unless explicitly
      toggled off); `"enabled": false` skips it.
  <configDir>/plugins/cache/<plugin-name>/
      -- the plugin's own files, when installed_plugins.json doesn't
      carry an explicit "path" for it. A manifest entry whose resolved
      directory doesn't exist on disk is skipped (a stale/half-removed
      install must never crash a session or try to spawn nothing).
  <plugin_root>/.mcp.json                    {"mcpServers": {...}}, OR
  <plugin_root>/.claude-plugin/plugin.json   {"mcpServers": {...}, ...}
      -- `.mcp.json` is checked FIRST (same shape as a project's own);
      `plugin.json`'s own top-level `mcpServers` key is the fallback (a
      plugin manifest can declare its servers directly instead of
      shipping a separate `.mcp.json`).

Each discovered server becomes an ordinary `McpServerConfig` (scope=
"plugin"), named `plugin_<plugin>_<server>` (sanitised piecewise via
`manager.sanitize_name`, same as any other server name) so its tools land
on the wire as `mcp__plugin_<plugin>_<server>__<tool>` [task brief;
binary-facts sec.9's own `mcp__<server>__<tool>` pattern, applied to this
synthetic server name] through the ordinary `manager.mcp_tool_name` path --
nothing downstream (manager.py/mcp_tool.py/catalog.py) needs to know a
server came from a plugin at all. `${CLAUDE_PLUGIN_ROOT}` is expanded, in
every field the normal `${VAR}` pipeline covers (command/args/env/url/
headers), to THAT one plugin's own root directory, on top of the caller's
own effective env -- inherently per-plugin, so this module does that
expansion itself rather than leaving it to `manager.py`'s single global
`env_for_expansion` pass (which has no one answer for a directory that
differs per plugin).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from rolo_claude.config.paths import claude_config_dir


def plugins_dir() -> Path:
    return claude_config_dir() / "plugins"


def installed_plugins_manifest_path() -> Path:
    return plugins_dir() / "installed_plugins.json"


def load_installed_plugins() -> dict:
    """`{}` if missing/invalid -- never raises (same contract as every
    other small-JSON-file loader in this codebase, e.g.
    `mcp_setup.load_mcp_approvals`)."""
    path = installed_plugins_manifest_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _plugin_roots(manifest: dict) -> "list[tuple[str, Path]]":
    """`[(plugin_name, root_dir), ...]` for every enabled plugin whose
    root directory actually exists on disk."""
    plugins = manifest.get("plugins")
    if not isinstance(plugins, dict):
        return []
    cache_dir = plugins_dir() / "cache"
    out = []
    for name, entry in plugins.items():
        if not isinstance(name, str) or not name:
            continue
        entry = entry if isinstance(entry, dict) else {}
        if entry.get("enabled") is False:
            continue
        explicit_path = entry.get("path")
        root = Path(explicit_path) if explicit_path else (cache_dir / name)
        if root.is_dir():
            out.append((name, root))
    return out


def _read_plugin_mcp_servers(plugin_root: Path) -> dict:
    """`.mcp.json` first, else `.claude-plugin/plugin.json`'s own
    top-level `mcpServers` key; `{}` (never raises) if neither exists,
    parses, or declares any servers."""
    for candidate in (plugin_root / ".mcp.json", plugin_root / ".claude-plugin" / "plugin.json"):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        servers = data.get("mcpServers")
        if isinstance(servers, dict) and servers:
            return servers
    return {}


def plugin_server_name(plugin: str, server: str) -> str:
    """`plugin_<plugin>_<server>`, sanitised piecewise the same way any
    other server name is."""
    from rolo_claude.mcp.manager import sanitize_name
    return f"plugin_{sanitize_name(plugin)}_{sanitize_name(server)}"


def discover_plugin_mcp_servers(*, env: Optional[dict] = None) -> "tuple[dict, list]":
    """`(name -> McpServerConfig, notices)` for every MCP server every
    installed, enabled plugin declares. `env` is the caller's own
    effective env (settings-resolved) for `${VAR}` expansion, augmented
    per-plugin with `CLAUDE_PLUGIN_ROOT`. Never raises -- a malformed
    plugin entry is skipped with a notice, not a crashed session."""
    from rolo_claude.mcp.manager import expand_config, parse_server

    notices: list = []
    resolved: dict = {}
    manifest = load_installed_plugins()
    for plugin_name, plugin_root in _plugin_roots(manifest):
        servers = _read_plugin_mcp_servers(plugin_root)
        if not servers:
            continue
        plugin_env = dict(env or {})
        plugin_env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        for server_name, raw in servers.items():
            wire_server_name = plugin_server_name(plugin_name, server_name)
            cfg = parse_server(wire_server_name, raw, scope="plugin", source_path=str(plugin_root))
            if cfg is None:
                notices.append(f"plugin {plugin_name!r}: server {server_name!r} entry is malformed, skipped")
                continue
            cfg.plugin = plugin_name
            expanded, warns = expand_config(cfg, plugin_env)
            for w in warns:
                notices.append(f"plugin {plugin_name!r} server {server_name!r}: {w}")
            resolved[wire_server_name] = expanded
    return resolved, notices

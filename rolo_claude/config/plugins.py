"""rolo_claude.config.plugins -- discovery of plugin-provided MCP servers
under ~/.claude/plugins/ (H3b closing requirement: "every server a user
adds to Claude Code must work"). Installed Claude Code PLUGINS can bundle
their own MCP servers, same as a user's own .mcp.json/~/.claude.json
entries -- none of that was wired into this harness before this module.

u2-h3b finding 11: the FIRST version of this module invented a layout that
never matched 2.1.281's real one -- verified against rolo's own
`claude-plugins-official` marketplace clone (`~/.claude/plugins/
marketplaces/claude-plugins-official/`, no plugin actually INSTALLED
there, only cloned, as of 2026-09-24) and the review's own binary-derived
facts. The real layout:

  <configDir>/plugins/installed_plugins.json
      V1 (this module's original, still accepted):
          {"plugins": {"<plugin-name>": {"enabled": bool, "path"?: str}}}
      V2 (2.1.281's real shape):
          {"version": 2, "plugins": {"<name>@<marketplace>": {
              "installPath": str, ...}}}
      -- V1's plugin root is an explicit "path", else `cache/<name>`; V2's
      is `installPath` (no fallback -- the binary always writes one). A V2
      key's `@<marketplace>` suffix is stripped for the PLUGIN NAME (wire
      server naming, doctor output, etc. all use the bare name) but kept
      verbatim for the `enabledPlugins` settings lookup below (two
      marketplaces can offer a same-named plugin without colliding).
      Detected PER MANIFEST via a top-level `"version": 2` -- V1 has none.
  Enablement:
      V1: the entry's own `"enabled"` field (missing == on, matching
      "installed == on unless explicitly toggled off"); `"enabled": false`
      skips it.
      V2: gated by the resolved settings' `enabledPlugins` (a `{"<name>@
      <marketplace>": bool}` map Claude Code itself writes when a plugin
      is installed/toggled) -- a key PRESENT there is authoritative; a
      plugin the settings say nothing about at all falls back to the
      manifest entry's own optional `"enabled"` field, default True (same
      "installed == on" default as V1, since a freshly-cloned marketplace
      plugin with no settings entry yet must not silently vanish).
  A manifest entry whose resolved root directory doesn't exist on disk is
  skipped (a stale/half-removed install must never crash a session or try
  to spawn nothing).
  <plugin_root>/.mcp.json                    -- EITHER shape:
      {"mcpServers": {"<server>": {...}}}        (wrapped, a project's own
                                                   .mcp.json shape), OR
      {"<server>": {...}, ...}                   (BARE -- verified: 9 of
                                                   14 real `.mcp.json`
                                                   files in the marketplace
                                                   clone use this, incl.
                                                   github and playwright)
  <plugin_root>/.claude-plugin/plugin.json   {"mcpServers": ..., ...}
      -- checked only when `.mcp.json` doesn't exist or declares nothing.
      Its own `mcpServers` key is EITHER an inline dict (either shape
      above) OR a STRING -- a path, relative to the plugin root, to
      another JSON file declaring the servers (either shape again).

Each discovered server becomes an ordinary `McpServerConfig` (scope=
"plugin"), named `plugin_<plugin>_<server>` (`<plugin>` already stripped
of `@<marketplace>`; both halves sanitised piecewise via
`manager.sanitize_name`, same as any other server name) so its tools land
on the wire as `mcp__plugin_<plugin>_<server>__<tool>` -- verified against
the binary's own `mcp__plugin_claude-test_browser__...` shape (docs/
harness/review-findings-h3.md's "No findings in" list) -- through the
ordinary `manager.mcp_tool_name` path: nothing downstream (manager.py/
mcp_tool.py/catalog.py) needs to know a server came from a plugin at all.
`${CLAUDE_PLUGIN_ROOT}` is expanded, in every field the normal `${VAR}`
pipeline covers (command/args/env/url/headers), to THAT one plugin's own
root directory, on top of the caller's own effective env -- inherently
per-plugin, so this module does that expansion itself rather than leaving
it to `manager.py`'s single global `env_for_expansion` pass (which has no
one answer for a directory that differs per plugin).
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


def _v2_plugin_enabled(key: str, entry: dict, settings_raw: dict) -> bool:
    """finding 11: V2 enablement gate = settings `enabledPlugins` (a
    `{"<name>@<marketplace>": bool}` map). A key the settings mention at
    all is AUTHORITATIVE (even to explicitly turn a plugin off); a key
    they say nothing about falls back to the manifest entry's own
    optional `"enabled"` field, default True -- so a plugin the binary
    just cloned/installed, before the user ever touched a settings
    toggle, is still discovered (parity with V1's own "installed == on"
    default) rather than silently invisible until some settings write
    happens to mention it."""
    enabled_plugins = settings_raw.get("enabledPlugins")
    if isinstance(enabled_plugins, dict) and key in enabled_plugins:
        return bool(enabled_plugins[key])
    if isinstance(enabled_plugins, list):
        return key in enabled_plugins
    return entry.get("enabled", True) is not False


def _plugin_roots(manifest: dict, settings_raw: Optional[dict] = None) -> "list[tuple[str, Path]]":
    """`[(plugin_name, root_dir), ...]` for every enabled plugin whose
    root directory actually exists on disk -- `plugin_name` already has
    any `@<marketplace>` suffix stripped. Detects V1 vs V2 per-manifest
    via a top-level `"version": 2` (V1 manifests never set it)."""
    plugins = manifest.get("plugins")
    if not isinstance(plugins, dict):
        return []
    settings_raw = settings_raw if isinstance(settings_raw, dict) else {}
    is_v2 = manifest.get("version") == 2
    cache_dir = plugins_dir() / "cache"
    out = []
    for key, entry in plugins.items():
        if not isinstance(key, str) or not key:
            continue
        entry = entry if isinstance(entry, dict) else {}
        if is_v2:
            plugin_name = key.split("@", 1)[0]
            if not _v2_plugin_enabled(key, entry, settings_raw):
                continue
            install_path = entry.get("installPath")
            root = Path(install_path) if install_path else (cache_dir / plugin_name)
        else:
            plugin_name = key
            if entry.get("enabled") is False:
                continue
            explicit_path = entry.get("path")
            root = Path(explicit_path) if explicit_path else (cache_dir / plugin_name)
        if root.is_dir():
            out.append((plugin_name, root))
    return out


def _coerce_mcp_servers_map(data) -> dict:
    """finding 11: accepts EITHER Claude Code's wrapped `{"mcpServers":
    {...}}` shape OR a BARE `{server_name: entry, ...}` map -- verified
    against rolo's real `claude-plugins-official` marketplace clone: 9 of
    14 `.mcp.json` files (including github and playwright) use the bare
    form. A bare map is recognised by every one of its top-level values
    looking like a server entry (a dict) -- guards against misreading an
    unrelated flat JSON object (or `plugin.json`'s own name/description/
    author fields) as a server list."""
    if not isinstance(data, dict):
        return {}
    wrapped = data.get("mcpServers")
    if isinstance(wrapped, dict):
        return wrapped
    if data and all(isinstance(v, dict) for v in data.values()):
        return data
    return {}


def _load_json_object(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_plugin_mcp_servers(plugin_root: Path) -> dict:
    """`.mcp.json` first (wrapped or bare -- see `_coerce_mcp_servers_map`),
    else `.claude-plugin/plugin.json`'s own top-level `mcpServers` key,
    which is EITHER an inline map (either shape again) OR a STRING: a path
    (relative to `plugin_root`) to another JSON file declaring the
    servers, in either shape once loaded. `{}` (never raises) if nothing
    exists, parses, or declares any servers."""
    mcp_json = plugin_root / ".mcp.json"
    if mcp_json.exists():
        data = _load_json_object(mcp_json)
        servers = _coerce_mcp_servers_map(data) if data is not None else {}
        if servers:
            return servers

    plugin_json = plugin_root / ".claude-plugin" / "plugin.json"
    data = _load_json_object(plugin_json)
    if data is None:
        return {}
    mcp_field = data.get("mcpServers")
    if isinstance(mcp_field, dict):
        return mcp_field
    if isinstance(mcp_field, str) and mcp_field.strip():
        ref_data = _load_json_object(plugin_root / mcp_field)
        return _coerce_mcp_servers_map(ref_data) if ref_data is not None else {}
    return {}


def plugin_server_name(plugin: str, server: str) -> str:
    """`plugin_<plugin>_<server>`, sanitised piecewise the same way any
    other server name is."""
    from rolo_claude.mcp.manager import sanitize_name
    return f"plugin_{sanitize_name(plugin)}_{sanitize_name(server)}"


def discover_plugin_mcp_servers(*, env: Optional[dict] = None, settings: object = None) -> "tuple[dict, list]":
    """`(name -> McpServerConfig, notices)` for every MCP server every
    installed, enabled plugin declares. `env` is the caller's own
    effective env (settings-resolved) for `${VAR}` expansion, augmented
    per-plugin with `CLAUDE_PLUGIN_ROOT`. `settings` (a `config.settings.
    Settings`, or anything with a `.raw` dict, optional) is where a V2
    manifest's `enabledPlugins` gate is read from -- omitted, a V2
    manifest falls back to each entry's own optional `"enabled"` field
    (see `_v2_plugin_enabled`). Never raises -- a malformed plugin entry
    is skipped with a notice, not a crashed session."""
    from rolo_claude.mcp.manager import expand_config, parse_server

    notices: list = []
    resolved: dict = {}
    settings_raw = getattr(settings, "raw", None)
    settings_raw = settings_raw if isinstance(settings_raw, dict) else {}
    manifest = load_installed_plugins()
    for plugin_name, plugin_root in _plugin_roots(manifest, settings_raw):
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

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
      V2 (2.1.281's real shape -- finding 8, h4-h5-h3c review, supersedes
      the single-dict shape this module originally guessed at):
          {"version": 2, "plugins": {"<name>@<marketplace>": [
              {"scope": "user"|"project"|"local", "projectPath"?: str,
               "installPath": str, "version": str, ...}, ...]}}
      -- `plugins[key]` is an ARRAY of SCOPED install records, one per
      scope the plugin is installed at, NOT a single dict. A "user"-scope
      record always applies; a "project"/"local" one only applies when
      its own `projectPath` matches the CURRENT session's cwd (a plugin
      installed for one repo must not leak into an unrelated one). Each
      record's own `installPath` is its root (no fallback -- the binary
      always writes one; a defensive fallback for a record missing it
      uses the real cache layout, `cache/<marketplace>/<plugin>/
      <version>`, never V1's `cache/<name>`). A V2 key's `@<marketplace>`
      suffix is stripped for the PLUGIN NAME (wire server naming, doctor
      output, etc. all use the bare name) but kept verbatim for the
      `enabledPlugins` settings lookup below (two marketplaces can offer a
      same-named plugin without colliding). Detected PER MANIFEST via a
      top-level `"version": 2` -- V1 has none.
  Enablement:
      V1: the entry's own `"enabled"` field (missing == on, matching
      "installed == on unless explicitly toggled off"); `"enabled": false`
      skips it.
      V2: gated ENTIRELY by the resolved settings' `enabledPlugins` (a
      `{"<name>@<marketplace>": bool}` map Claude Code itself writes when
      a plugin is installed/toggled) -- a key PRESENT there is
      authoritative; a plugin the settings say nothing about at all
      defaults to True ("installed == on", same default as V1) -- the
      real V2 record shape carries no per-record `"enabled"` field to
      fall back to.
  A record whose resolved root directory doesn't exist on disk is skipped
  (a stale/half-removed install must never crash a session or try to
  spawn nothing).
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


def _v2_plugin_enabled(key: str, settings_raw: dict) -> bool:
    """finding 8/11: V2 enablement gate = settings `enabledPlugins` (a
    `{"<name>@<marketplace>": bool}` map). A key the settings mention at
    all is AUTHORITATIVE (even to explicitly turn a plugin off); a key
    they say nothing about defaults to True -- "installed == on" until the
    user (or the binary itself, on install) actually toggles it, matching
    V1's own default. finding 8: the real V2 record shape (`{scope,
    projectPath?, installPath, version, …}`, an ARRAY per plugin -- see
    `_plugin_roots`) carries no per-record `"enabled"` field at all, so
    (unlike the pre-finding-8 version) this never falls back to reading
    one off an invented single-dict entry."""
    enabled_plugins = settings_raw.get("enabledPlugins")
    if isinstance(enabled_plugins, dict) and key in enabled_plugins:
        return bool(enabled_plugins[key])
    if isinstance(enabled_plugins, list):
        return key in enabled_plugins
    return True


def _v2_record_root(key: str, plugin_name: str, record: dict, *, cwd: Optional[Path]) -> Optional[Path]:
    """One scoped install record's own root directory, or None if this
    record doesn't apply to the CURRENT session (`scope` is "project"/
    "local" and its `projectPath` doesn't match `cwd`) or is malformed.
    finding 8: `scope == "user"` records always apply; "project"/"local"
    records only apply when `projectPath` resolves to the SAME directory
    as `cwd` (a project-scoped plugin installed in one repo must not leak
    into an unrelated one)."""
    if not isinstance(record, dict):
        return None
    scope = record.get("scope")
    if scope in ("project", "local"):
        project_path = record.get("projectPath")
        if not project_path or cwd is None:
            return None
        try:
            if Path(project_path).resolve() != Path(cwd).resolve():
                return None
        except OSError:
            return None
    elif scope != "user":
        return None  # an unrecognised scope is skipped defensively, never guessed at
    install_path = record.get("installPath")
    if install_path:
        return Path(install_path)
    # finding 8: the binary always writes installPath in practice, but a
    # defensive fallback still uses the REAL cache layout --
    # cache/<marketplace>/<plugin>/<version> -- never the old invented
    # cache/<plugin> (V1's own layout, wrong for V2).
    marketplace = key.split("@", 1)[1] if "@" in key else ""
    version = record.get("version")
    if not marketplace or not version:
        return None
    return plugins_dir() / "cache" / marketplace / plugin_name / str(version)


def _plugin_roots(manifest: dict, settings_raw: Optional[dict] = None, *, cwd: Optional[Path] = None) -> "list[tuple[str, Path]]":
    """`[(plugin_name, root_dir), ...]` for every enabled plugin whose
    root directory actually exists on disk -- `plugin_name` already has
    any `@<marketplace>` suffix stripped. Detects V1 vs V2 per-manifest
    via a top-level `"version": 2` (V1 manifests never set it).

    finding 8 (major, h4-h5-h3c review): a V2 manifest's `plugins[key]` is
    an ARRAY of SCOPED install records (`[{scope, projectPath?,
    installPath, version, …}, ...]`, per the binary's own V1->V2
    converter), never a single dict -- the pre-fix version read it as one
    dict, so `isinstance(entry, dict)` was always False for a real
    `claude plugin install` manifest, `entry` silently became `{}`, and
    the root fell back to the WRONG cache layout (`cache/<name>` instead
    of `cache/<marketplace>/<plugin>/<version>`), meaning every plugin's
    MCP servers AND hooks were silently skipped end to end. A plugin can
    have several applicable records (e.g. a "user" one plus a "project"
    one for the CURRENT repo) -- every one that applies gets its own
    `(plugin_name, root)` entry (a plugin installed at more than one
    applicable scope just gets discovered twice, same servers/hooks
    merged twice -- harmless, `merge_hook_maps`/dict-keyed MCP configs are
    naturally idempotent)."""
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
        if is_v2:
            plugin_name = key.split("@", 1)[0]
            if not _v2_plugin_enabled(key, settings_raw):
                continue
            records = entry if isinstance(entry, list) else ([entry] if isinstance(entry, dict) else [])
            for record in records:
                root = _v2_record_root(key, plugin_name, record, cwd=cwd)
                if root is not None and root.is_dir():
                    out.append((plugin_name, root))
        else:
            # finding 21 (h5b review): a V1 record uses the same
            # `<name>@<marketplace>` key and `installPath` field as a V2
            # one -- only the outer shape differs (a single dict, never an
            # array of scoped records). Reading the invented `path` field
            # (never written by the real binary) instead of `installPath`,
            # and keeping the `@marketplace` suffix on `plugin_name`, meant
            # a V1 manifest 2.1.28x has not yet migrated to V2 discovered
            # no plugins at all.
            entry = entry if isinstance(entry, dict) else {}
            plugin_name = key.split("@", 1)[0]
            if entry.get("enabled") is False:
                continue
            install_path = entry.get("installPath")
            root = Path(install_path) if install_path else (cache_dir / plugin_name)
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


def discover_plugin_mcp_servers(*, env: Optional[dict] = None, settings: object = None,
                                 cwd: Optional[Path] = None) -> "tuple[dict, list]":
    """`(name -> McpServerConfig, notices)` for every MCP server every
    installed, enabled plugin declares. `env` is the caller's own
    effective env (settings-resolved) for `${VAR}` expansion, augmented
    per-plugin with `CLAUDE_PLUGIN_ROOT`. `settings` (a `config.settings.
    Settings`, or anything with a `.raw` dict, optional) is where a V2
    manifest's `enabledPlugins` gate is read from. `cwd` (finding 8) is
    matched against a V2 project/local-scoped record's own `projectPath`
    -- omitted, only `scope == "user"` records are ever discovered. Never
    raises -- a malformed plugin entry is skipped with a notice, not a
    crashed session."""
    from rolo_claude.mcp.manager import expand_config, parse_server

    notices: list = []
    resolved: dict = {}
    settings_raw = getattr(settings, "raw", None)
    settings_raw = settings_raw if isinstance(settings_raw, dict) else {}
    manifest = load_installed_plugins()
    for plugin_name, plugin_root in _plugin_roots(manifest, settings_raw, cwd=cwd):
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

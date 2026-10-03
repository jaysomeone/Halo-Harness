"""halo_harness.mcp.explain -- "explain the zero" (Halo 2.0.1 gap-list
brief, W4b item 1 / "W4 MCP: claude.ai connectors bridge" item 1):
`/mcp`, `halo mcp list` and doctor's MCP line must say WHAT was searched
(user scope, project-local scope, this directory's own `.mcp.json`,
plugins, managed) and the count found in each, name another directory's
own `.mcp.json` when the user's own history shows one, and add the
claude.ai connectors `claude` itself reports -- never a bare 0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from halo_harness.config.paths import claude_json_path, lookup_project, normalize_cwd


def _count_json_dict(path: Path, key: Optional[str] = None) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return 0
    if key is not None:
        data = data.get(key) if isinstance(data, dict) else None
    return len(data) if isinstance(data, dict) else 0


def scope_rows(*, cwd: Path, claude_json: dict, settings=None) -> "list[dict]":
    """One row per scope `mcp.manager.resolve_server_configs` reads from,
    counting the RAW entries present there -- this is about what each
    place HOLDS, before cross-scope name precedence/policy filtering ever
    decides who wins."""
    proj = lookup_project(claude_json, cwd) if isinstance(claude_json, dict) else {}
    mcp_json_path = Path(cwd) / ".mcp.json"
    from halo_harness.mcp_setup import managed_mcp_json_path
    managed_path = managed_mcp_json_path()
    try:
        from halo_harness.config.plugins import discover_plugin_mcp_servers
        plugin_servers, _notices = discover_plugin_mcp_servers(settings=settings, cwd=cwd)
    except Exception:
        plugin_servers = {}
    user_servers = claude_json.get("mcpServers") if isinstance(claude_json, dict) else None
    local_servers = proj.get("mcpServers") if isinstance(proj, dict) else None
    return [
        {"scope": "user", "path": str(claude_json_path()),
         "count": len(user_servers) if isinstance(user_servers, dict) else 0},
        # No square brackets around the path: these lines are printed through
        # rich in `halo init`, where `[/home/...]` reads as a closing markup
        # tag (seen live on the Kali VM: MarkupError, init exited 1).
        {"scope": "project-local", "path": f"{claude_json_path()} projects entry for {normalize_cwd(cwd)}",
         "count": len(local_servers) if isinstance(local_servers, dict) else 0},
        {"scope": "this directory's .mcp.json", "path": str(mcp_json_path), "exists": mcp_json_path.exists(),
         "count": _count_json_dict(mcp_json_path, "mcpServers") if mcp_json_path.exists() else 0},
        {"scope": "plugins", "path": "installed, enabled plugins", "count": len(plugin_servers)},
        {"scope": "managed", "path": str(managed_path), "count": _count_json_dict(managed_path)},
    ]


def scope_summary_line(*, cwd: Path, claude_json: dict, settings=None) -> str:
    rows = scope_rows(cwd=cwd, claude_json=claude_json, settings=settings)
    parts = "; ".join(f"{r['scope']} ({r['path']}): {r['count']}" for r in rows)
    return f"Searched -- {parts}."


def other_project_mcp_jsons(*, cwd: Path, claude_json: dict, limit: int = 5) -> "list[str]":
    """Directories OTHER than this one that the user's own `~/.claude.json`
    "projects" history remembers and that still have their own `.mcp.json`
    on disk right now (gap-list brief's own example: "~/other-project/
    .mcp.json: loads only when halo runs there")."""
    if not isinstance(claude_json, dict):
        return []
    projects = claude_json.get("projects")
    if not isinstance(projects, dict):
        return []
    here = normalize_cwd(cwd)
    found: list = []
    for key in sorted(k for k in projects if isinstance(k, str)):
        if normalize_cwd(key) == here:
            continue
        candidate = Path(key) / ".mcp.json"
        try:
            if candidate.exists():
                found.append(f"{candidate}: loads only when halo runs there")
        except OSError:
            continue
        if len(found) >= limit:
            break
    return found


def connector_lines() -> "list[str]":
    """claude.ai connectors `claude` itself reports. `explain_lines`'s own
    caller (`halo mcp list` without `--refresh`) already ran `connectors_
    bridge.ensure_discovered_synchronously_if_cold()` first, so by the time
    THIS reads the cache it's real whenever discovery was eligible at all
    -- this is cache-only and never spawns anything itself, for `/mcp`
    (no cold-start call of its own; always cache-only) and any other
    caller that skips that step.

    W5 ("connector cold start" / `halo mcp list` without `--refresh`):
    `unavailable_reason()` returns `None` whenever discovery LOOKS eligible
    (bridge enabled, `claude` installed, not gateway-driven, no confirmed
    "not a claude.ai login") -- including the exact moment right after a
    fresh cache file is written with zero entries (a real claude.ai login
    with genuinely no connectors configured, or a synchronous cold-start
    discovery that just ran and found nothing). Never silence in that
    state either -- mirrors doctor's own `_check_mcp_connectors` wording
    exactly, so the three surfaces (`/mcp`, `halo mcp list`, doctor) never
    disagree about what an empty-but-eligible cache means."""
    from halo_harness.mcp import connectors_bridge
    connectors = connectors_bridge.get_connectors()
    if connectors:
        return [connectors_bridge.status_line(c) for c in connectors]
    reason = connectors_bridge.unavailable_reason()
    if reason:
        return [reason]
    if connectors_bridge.bridge_enabled() and connectors_bridge.claude_binary_available():
        return ["claude.ai connectors: bridge enabled, none discovered yet (discovery runs in the "
                "background on the next launch, or `halo mcp list --refresh` now)."]
    return []


def explain_lines(*, cwd: Path, claude_json: dict, settings=None) -> "list[str]":
    """The full "explain the zero" preamble, shared by `halo mcp list`,
    `/mcp`/`-p "/mcp"` and doctor's MCP line so the three can never
    disagree about what was searched or found."""
    lines = [scope_summary_line(cwd=cwd, claude_json=claude_json, settings=settings)]
    for other in other_project_mcp_jsons(cwd=cwd, claude_json=claude_json):
        lines.append(f"Other directory with its own config: {other}")
    lines.extend(connector_lines())
    return lines

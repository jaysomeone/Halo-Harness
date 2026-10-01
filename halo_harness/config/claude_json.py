from __future__ import annotations
import json
import os
from pathlib import Path
from halo_harness.config.paths import (
    bridge_home,
    claude_json_path,
    normalize_cwd,
    lookup_project,
)


def load_bridge_trust() -> dict:
    """Load ~/.halo/trust.json (our OWN trust dialog's storage, never
    written to by anything but our own future trust-prompt code) fresh every
    call; {} if missing/invalid. Never raises."""
    path = bridge_home() / "trust.json"
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_claude_json() -> dict:
    """Load ~/.claude.json fresh every call; {} if missing or invalid. Not
    cached on purpose -- these are small local files and a test may write
    then immediately re-read one within the same process, so freshness beats
    the (negligible) cost of re-parsing.

    Linux/H4 must-do: read as `utf-8-sig`, not plain `utf-8` -- `mcp add`/
    `mcp remove` (mcp_cli.py's `_write_claude_json_raw`) preserve a BOM if
    the file already had one, but plain `utf-8` chokes on the BOM bytes
    themselves (`json.loads` sees `﻿{...}` and raises), so a BOM'd
    `~/.claude.json` silently read as `{}` here -- every configured MCP
    server, project, etc. -- even though the file itself was perfectly
    valid JSON-with-BOM the whole time. `utf-8-sig` strips a leading BOM
    when present and behaves exactly like `utf-8` when it isn't, matching
    every other `~/.claude.json`/`.mcp.json` reader in this codebase
    (mcp/manager.py's `_load_json_file`, config/plugins.py)."""
    path = claude_json_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def is_trusted(cwd: str | Path, claude_json: dict,
               bridge_trust: dict | None = None) -> bool:
    """Return True if any of the three trust conditions are met. Ports
    claude.exe 2.1.281's `FD()/lb()/ub()` [finding 5]: the ancestor walk for
    condition (a) is bounded by the git toplevel WHEN INSIDE a repo, but
    walks all the way to the filesystem root otherwise (not just cwd itself
    -- a non-git folder trusted at `D:/notes` must still read as trusted
    from `D:/notes/sub`)."""
    cwd_path = Path(cwd).absolute()

    def has_trust_in_project(project_cwd: Path) -> bool:
        proj = lookup_project(claude_json, project_cwd)
        return bool(proj.get("hasTrustDialogAccepted"))

    git_toplevel = None
    current = cwd_path
    while True:
        if (current / ".git").exists():
            git_toplevel = current
            break
        parent = current.parent
        if parent == current:  # reached the filesystem root
            break
        current = parent

    # Walk cwd -> ancestors, stopping at git_toplevel (inclusive) when found,
    # else continuing all the way to the filesystem root.
    check_path = cwd_path
    while True:
        if has_trust_in_project(check_path):
            return True
        if git_toplevel is not None and check_path == git_toplevel:
            break
        parent = check_path.parent
        if parent == check_path:  # reached the filesystem root
            break
        check_path = parent

    # Condition (b): CLAUDE_CODE_SANDBOXED set to ANY non-empty value counts
    # (finding 5: not just the literal string "1").
    if os.environ.get("CLAUDE_CODE_SANDBOXED"):
        return True

    # Condition (c): our own trust.json entry for the normalized cwd.
    trust_dict = bridge_trust if bridge_trust is not None else load_bridge_trust()
    norm_cwd = normalize_cwd(cwd)
    if trust_dict.get(norm_cwd):
        return True

    return False


def mcp_servers_for(cwd: str | Path, claude_json: dict) -> dict:
    """Merge user-scope and project-scope MCP servers (project wins collisions)."""
    try:
        user_servers = claude_json.get("mcpServers", {})
        if not isinstance(user_servers, dict):
            user_servers = {}

        proj = lookup_project(claude_json, cwd)
        proj_servers = proj.get("mcpServers", {})
        if not isinstance(proj_servers, dict):
            proj_servers = {}

        # Shallow merge: project entries win collisions
        result = user_servers.copy()
        for key, val in proj_servers.items():
            result[key] = val
        return result
    except Exception:
        return {}

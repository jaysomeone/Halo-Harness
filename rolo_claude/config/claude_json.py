from __future__ import annotations
import json
import os
from pathlib import Path
from rolo_claude.config.paths import (
    claude_json_path,
    normalize_cwd,
    lookup_project,
    project_key_candidates,
)


def load_claude_json() -> dict:
    """Load ~/.claude.json fresh every call; {} if missing or invalid. Not
    cached on purpose -- these are small local files and a test may write
    then immediately re-read one within the same process, so freshness beats
    the (negligible) cost of re-parsing."""
    path = claude_json_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def is_trusted(cwd: str | Path, claude_json: dict,
               bridge_trust: dict | None = None) -> bool:
    """Return True if any of the three trust conditions are met."""
    cwd_path = Path(cwd)
    # Condition (a): hasTrustDialogAccepted for cwd or ancestor up to git toplevel
    def has_trust_in_project(project_cwd: Path) -> bool:
        proj = lookup_project(claude_json, project_cwd)
        return bool(proj.get("hasTrustDialogAccepted"))

    # Walk up to git toplevel
    git_toplevel = None
    current = cwd_path.absolute()
    while True:
        git_check = current / ".git"
        if git_check.exists() or git_check.is_file():
            git_toplevel = current
            break
        parent = current.parent
        if parent == current:  # reached root
            break
        current = parent

    if git_toplevel is not None:
        # Check cwd and ancestors up to git_toplevel inclusive
        check_path = cwd_path.absolute()
        while True:
            if has_trust_in_project(check_path):
                return True
            if check_path == git_toplevel:
                break
            parent = check_path.parent
            if parent == check_path:  # should not happen given git_toplevel exists
                break
            check_path = parent
    else:
        # No git repo, only check cwd itself
        if has_trust_in_project(cwd_path):
            return True

    # Condition (b): CLAUDE_CODE_SANDBOXED=1
    if os.environ.get("CLAUDE_CODE_SANDBOXED") == "1":
        return True

    # Condition (c): bridge_trust entry for normalized cwd
    trust_dict = bridge_trust if bridge_trust is not None else {}
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

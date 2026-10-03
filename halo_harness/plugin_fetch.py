"""halo_harness.plugin_fetch -- W4a `--plugin-dir`/`--plugin-url`: "load a
plugin's skills, commands, agents, hooks and MCP servers from a directory or
a git URL" (plans/2.0.1-w4-plan.md W4a item 2). `--plugin-dir` needs no
fetching at all -- its path IS the plugin root, handed straight to
`config.agents_md.discover_agents`'s own `plugin_roots=` kwarg (the one
precedence tier that already exists for "a plugin" generally). `--plugin-url`
(a git URL, per the gap list's own gloss -- Claude Code's real flag fetches
a .zip; this harness takes the simpler, already-everywhere-available git
clone instead) is cloned ONCE per URL into a cache keyed by its own hash, so
a repeat launch with the same URL reuses the existing checkout rather than
re-cloning every single time.

THIN, v1 scope: only the AGENTS precedence tier (`discover_agents`'s own
`plugin_roots=`) is wired for a CLI-supplied plugin directory/URL this
round -- a plugin's skills/hooks/MCP servers loading from `--plugin-dir`/
`--plugin-url` specifically (as opposed to an INSTALLED plugin, which
`config/plugins.py` already covers independently) is left for a follow-up;
see the worker report.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Optional


def _clone_cache_dir(state_dir: Path) -> Path:
    d = Path(state_dir) / "plugins-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def clone_plugin_url(url: str, state_dir: Path, *, timeout: float = 60.0) -> Optional[Path]:
    """Shallow-clones `url` into `<state_dir>/plugins-cache/<sha1(url)[:16]>`
    the FIRST time it's seen; a later call with the SAME url reuses the
    existing checkout unchanged (never re-clones, never `git pull`s -- a
    plugin's own version is pinned to whatever was there on first use,
    same spirit as this harness's other caches). Returns `None` (never
    raises) when `git` is missing or the clone fails -- the caller's own
    notice is what the user actually sees; this stays silent about why."""
    digest = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:16]
    dest = _clone_cache_dir(state_dir) / digest
    if (dest / ".git").is_dir():
        return dest
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not dest.is_dir():
        return None
    return dest


def resolve_plugin_roots(plugin_dirs: list, plugin_urls: list, *, state_dir: Path) -> "list[str]":
    """`cli_flags["plugin_dir"] + cli_flags["plugin_url"]` -> a flat list of
    real, existing directory paths (string form, `discover_agents`'s own
    `plugin_roots` shape) -- an unresolvable `--plugin-dir` (doesn't exist)
    or a failed `--plugin-url` clone is dropped silently (best-effort,
    matching every other plugin-loading path in this harness)."""
    roots: "list[str]" = []
    for d in plugin_dirs or []:
        p = Path(d)
        if p.is_dir():
            roots.append(str(p))
    for url in plugin_urls or []:
        cloned = clone_plugin_url(url, state_dir)
        if cloned is not None:
            roots.append(str(cloned))
    return roots

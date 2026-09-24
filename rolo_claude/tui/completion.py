"""rolo_claude.tui.completion -- `/` (slash command) and `@` (path) prefix
completion for PromptInput's popup. Pure functions, no textual import, so
they're unit-testable directly. `@path` completion is an `os.scandir` walk
(never `Path.rglob`, which has no early-exit/prune hook) capped at 20k
directory entries visited (D-TUI: "os.scandir walk with prunes, 20k cap").
"""

from __future__ import annotations

import os

SCAN_CAP = 20_000
MAX_RESULTS = 50

_PRUNE_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".idea",
    ".vscode", ".tox", "egg-info", ".egg-info",
})


def _is_pruned(name: str) -> bool:
    return name in _PRUNE_DIRS or name.endswith(".egg-info")


def complete_slash(prefix: str, registry) -> list:
    """`[(invocation, description), ...]` for every command whose name (or
    alias) starts with `prefix` (already stripped of its leading '/' by the
    caller, or not -- `Registry.complete` strips it itself). `registry` may
    be None (no commands discovered yet, or a minimal test double)."""
    if registry is None:
        return []
    return [(cmd.invocation(), cmd.description) for cmd in registry.complete(prefix)]


def complete_at_path(prefix: str, cwd: str) -> "list[str]":
    """Paths (relative to `cwd`, forward-slashed) whose text (basename, or
    the trailing path segment being typed) starts with the last path
    segment of `prefix`, case-insensitively. `prefix` may itself contain
    slashes (`src/too`) -- everything before the last `/` names the
    directory to list; only ONE level is scanned per call (the popup
    re-queries as the user types past a `/`), matching a normal shell-style
    completion feel rather than a full recursive search."""
    prefix = prefix or ""
    if "/" in prefix:
        rel_dir, partial = prefix.rsplit("/", 1)
    else:
        rel_dir, partial = "", prefix
    base = os.path.join(cwd, rel_dir) if rel_dir else cwd
    partial_low = partial.lower()

    results: list = []
    visited = 0
    try:
        with os.scandir(base) as it:
            for entry in it:
                visited += 1
                if visited > SCAN_CAP:
                    break
                name = entry.name
                if name.startswith(".") and not partial.startswith("."):
                    continue
                if not name.lower().startswith(partial_low):
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if is_dir and _is_pruned(name):
                    continue
                rel = f"{rel_dir}/{name}" if rel_dir else name
                results.append(rel + "/" if is_dir else rel)
                if len(results) >= MAX_RESULTS:
                    break
    except OSError:
        return []
    results.sort(key=str.lower)
    return results


def current_token(text: str, cursor_pos: int) -> "tuple[str, int, str]":
    """`(kind, start_index, token_text)` for the `/`- or `@`-prefixed token
    the cursor is currently inside of, scanning back from `cursor_pos` in
    the flat string `text` to the nearest preceding whitespace or start of
    string. `kind` is "slash" (only valid when the token starts at index 0
    -- a real Claude Code slash command is only recognized as the first
    thing on the line), "at", or "" (no completion applies here)."""
    start = cursor_pos
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    token = text[start:cursor_pos]
    if token.startswith("/") and start == 0:
        return "slash", start, token[1:]
    if token.startswith("@"):
        return "at", start, token[1:]
    return "", start, token

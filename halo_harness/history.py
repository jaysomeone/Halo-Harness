"""halo_harness.history -- merged prompt history (U0 scope C).

Two JSONL files, IDENTICAL schema (`{display, pastedContents, project,
sessionId, timestamp}` -- Claude Code's own `~/.claude/history.jsonl`
shape, finding B):

  * `~/.claude/history.jsonl`     -- Claude Code's own file, READ-ONLY,
                                     never written by this module.
  * `~/.halo/history.jsonl` -- this harness's own file, read AND
                                     appended to.

`project` is a cwd string that may have been written with either path
separator form (finding B: ".claude.json"'s own `projects` keys have the
same mixed-separator problem) -- filtering by project normalizes both the
stored value and the query cwd via `config.paths.normalize_cwd` before
comparing, so a caller never has to care which form a given line used.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from halo_harness.config.paths import claude_config_dir, bridge_home, normalize_cwd

HISTORY_SCHEMA_KEYS = ("display", "pastedContents", "project", "sessionId", "timestamp")


def claude_history_path() -> Path:
    """Claude Code's own history file -- read-only."""
    return claude_config_dir() / "history.jsonl"


def rolo_history_path() -> Path:
    """This harness's own history file -- read/append."""
    return bridge_home() / "history.jsonl"


def _read_jsonl(path: Path) -> list:
    """Every well-formed JSON-object line in `path`; a malformed line is
    skipped rather than aborting the whole read (a history file is
    append-only and may have been truncated mid-write by a crash/kill)."""
    if not path.exists():
        return []
    entries = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def _matches_project(entry: dict, normalized_cwd: str) -> bool:
    project = entry.get("project")
    if not isinstance(project, str) or not project:
        return False
    return normalize_cwd(project) == normalized_cwd


def load_merged_history(cwd: Optional[str] = None, *, limit: Optional[int] = None) -> list:
    """Both files' entries, oldest first (stable sort by `timestamp` when
    present -- entries missing it sort as if timestamp 0, i.e. first).
    `cwd`, when given, filters to entries whose `project` normalizes to the
    same path as `cwd` (either separator form matches -- see module
    docstring). `limit`, when given, returns only the LAST `limit` entries
    (the ones nearest "now", matching what an up-arrow recall wants)."""
    entries = _read_jsonl(claude_history_path()) + _read_jsonl(rolo_history_path())

    if cwd is not None:
        normalized_cwd = normalize_cwd(cwd)
        entries = [e for e in entries if _matches_project(e, normalized_cwd)]

    entries.sort(key=lambda e: e.get("timestamp") or 0)

    if limit is not None and limit >= 0:
        entries = entries[-limit:]
    return entries


def append_history_entry(display: str, cwd: str, *, session_id: Optional[str] = None,
                          pasted_contents: Optional[dict] = None, timestamp: Optional[float] = None) -> dict:
    """Append one entry (Claude Code's own schema) to OUR history file
    (`~/.halo/history.jsonl`) -- never Claude Code's. `project` is
    stored as given (normalization only ever happens at READ time, matching
    how Claude Code's own file already has mixed forms recorded historically
    -- rewriting past entries is never this module's job). Returns the
    entry actually written."""
    entry = {
        "display": display,
        "pastedContents": pasted_contents or {},
        "project": str(cwd),
        "sessionId": session_id or "",
        "timestamp": timestamp if timestamp is not None else time.time(),
    }
    path = rolo_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return entry

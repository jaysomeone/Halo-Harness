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

# W2c: verified live on the Kali VM -- Claude Code's own `~/.claude/
# history.jsonl` stores `timestamp` in MILLISECONDS (e.g. 1790623928405,
# Claude Code's own schema), while this harness's `append_history_entry`
# used to write SECONDS (`time.time()`, e.g. 1790882425.67). Sorting both
# files' entries by the raw value (the old code) therefore put every single
# Claude Code entry after every halo entry regardless of real time (a ms
# value is ~1000x a seconds one for the same instant) -- Up always recalled
# the last `claude` prompt for that directory, never the one just typed
# here. A real seconds-since-epoch value won't reach this threshold until
# the year 5138; any observed value above it is unambiguously milliseconds.
_MS_THRESHOLD = 1e11


def _normalize_timestamp(value) -> float:
    """`value` (either unit, or missing/malformed) -> real seconds-since-
    epoch, for SORTING only -- never written back to any entry. `append_
    history_entry` below now writes milliseconds for its own "now" default,
    matching Claude Code's schema, but an explicit caller-supplied
    `timestamp` (every existing test's own small, relative values included)
    is stored exactly as given -- this function is what lets BOTH units
    coexist and still sort correctly by real time."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0.0
    value = float(value)
    return value / 1000.0 if value > _MS_THRESHOLD else value


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
    """Both files' entries, oldest first by REAL time -- `timestamp` is
    normalized at READ time (`_normalize_timestamp`: a value above 1e11 is
    milliseconds, divided by 1000; entries missing it sort as if timestamp
    0, i.e. first) so a mixed-unit merge (Claude Code's own file in
    milliseconds, ours in seconds or, going forward, also milliseconds)
    always sorts by when each entry was actually typed, never by raw
    magnitude. On an exact tie (equal normalized seconds) this harness's
    OWN entries sort last -- they're the ones typed HERE, so an Up-arrow
    recall prefers them over a same-instant Claude Code entry. `cwd`, when
    given, filters to entries whose `project` normalizes to the same path
    as `cwd` (either separator form matches -- see module docstring).
    `limit`, when given, returns only the LAST `limit` entries (the ones
    nearest "now", matching what an up-arrow recall wants)."""
    # origin 0 (Claude Code) sorts before origin 1 (ours) on a tie -- see
    # the docstring above. Tagged here, as a parallel (origin, entry) pair,
    # rather than mutating/annotating the entry dict itself, so every
    # returned entry's own keys stay exactly HISTORY_SCHEMA_KEYS.
    tagged = ([(0, e) for e in _read_jsonl(claude_history_path())]
              + [(1, e) for e in _read_jsonl(rolo_history_path())])

    if cwd is not None:
        normalized_cwd = normalize_cwd(cwd)
        tagged = [(origin, e) for origin, e in tagged if _matches_project(e, normalized_cwd)]

    tagged.sort(key=lambda pair: (_normalize_timestamp(pair[1].get("timestamp")), pair[0]))
    entries = [e for _origin, e in tagged]

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
        # W2c: MILLISECONDS from now on (Claude Code's own schema) for the
        # "now" default -- an explicit `timestamp` argument (every existing
        # caller that passes one, including every test fixture's own small
        # relative values) is stored exactly as given, never rescaled;
        # `_normalize_timestamp` above is what lets a reader sort both
        # units correctly regardless of which this entry used.
        "timestamp": timestamp if timestamp is not None else time.time() * 1000.0,
    }
    path = rolo_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return entry

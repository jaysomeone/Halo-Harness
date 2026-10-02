"""halo_harness.launch_state -- 2.0.1 W3a: "launch with the last session's
model and effort" (rolo, live: "have the last session's model choice and
effort start every time you launch it, it defers back to deepseek each
time"). Persists the last `/model`/`/effort` choice to `~/.halo/state.json`
(tmp + os.replace, same convention as `providers.databricks._atomic_write_
json`) -- NEVER to Claude Code's own files. Two scopes are kept per field:
`global` (the most recent choice made ANYWHERE) and `by_cwd` (keyed by a
normalized cwd string) -- `resolve_*`'s own `memory` parameter picks
cwd-then-global (the default) or global-only, matching the new
`model_memory` config key (`"cwd"`|`"global"`, default `"cwd"`).

This module only ever reads/writes `state.json` -- it knows nothing about
provider enablement or model parsing; `headless.build_session`'s own
precedence chain (flags > last_model/last_effort > config.json > settings.
json > provider default) is responsible for validating a persisted ref
still resolves (a provider disabled since it was saved falls through with
one notice) before ever using it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

STATE_FILENAME = "state.json"


def state_path() -> Path:
    from halo_harness.config.paths import bridge_home
    return bridge_home() / STATE_FILENAME


def _load() -> dict:
    path = state_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data: dict) -> None:
    """Best-effort, same contract every other small state writer here has
    (a read-only home, a full disk, ... must never crash a `/model`/
    `/effort` change) -- tmp file in the SAME directory + `os.replace`
    (atomic on both POSIX and Windows for a same-filesystem rename, so a
    reader never observes a truncated file mid-write)."""
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except OSError:
        pass


def _cwd_key(cwd: "str | Path") -> str:
    try:
        return str(Path(cwd).resolve())
    except OSError:
        return str(cwd)


def _record(field: str, entry: dict, *, cwd: "str | Path") -> None:
    data = _load()
    section = data.get(field)
    if not isinstance(section, dict):
        section = {}
    by_cwd = section.get("by_cwd")
    if not isinstance(by_cwd, dict):
        by_cwd = {}
    by_cwd[_cwd_key(cwd)] = entry
    section["by_cwd"] = by_cwd
    section["global"] = entry
    data[field] = section
    _write(data)


def record_last_model(ref: str, *, cwd: "str | Path") -> None:
    if not ref:
        return
    _record("last_model", {"ref": ref, "time": time.time(), "cwd": _cwd_key(cwd)}, cwd=cwd)


def record_last_effort(level: "Optional[str]", *, cwd: "str | Path") -> None:
    # A model with no adjustable effort at all clears to None -- still
    # worth recording (the NEXT model switched to in this cwd/globally
    # should not inherit a stale level from a completely different route).
    _record("last_effort", {"level": level, "time": time.time(), "cwd": _cwd_key(cwd)}, cwd=cwd)


def _resolve(field: str, cwd: "str | Path", *, memory: str, key: str) -> "Optional[str]":
    data = _load()
    section = data.get(field)
    if not isinstance(section, dict):
        return None
    if memory != "global":
        by_cwd = section.get("by_cwd")
        if isinstance(by_cwd, dict):
            entry = by_cwd.get(_cwd_key(cwd))
            if isinstance(entry, dict) and entry.get(key):
                return entry[key]
    glob = section.get("global")
    if isinstance(glob, dict) and glob.get(key):
        return glob[key]
    return None


def resolve_last_model(cwd: "str | Path", *, memory: str = "cwd") -> "Optional[str]":
    return _resolve("last_model", cwd, memory=memory, key="ref")


def resolve_last_effort(cwd: "str | Path", *, memory: str = "cwd") -> "Optional[str]":
    return _resolve("last_effort", cwd, memory=memory, key="level")

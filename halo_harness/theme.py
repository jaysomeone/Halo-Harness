"""halo_harness.theme -- theme name resolution + persistence (U0 scope D).

Precedence: `--theme` (CLI flag) > `HALO_THEME`/`CLAUDE_BRIDGE_THEME`/
`ROLO_CLAUDE_THEME` (env, checked in that order -- the 2.0.0 rename's new
canonical name first, both legacy names still honoured) > settings `theme`
> "claude-dark" (built-in default). Pure data -- Textual itself is NOT
imported here (U2's job); this module only decides WHICH theme name is
active and reads/writes the one place a `/theme` command would persist a
user's choice: `~/.halo/config.json`'s `"theme"` key (never Claude Code's
own settings.json -- that file is config we only ever READ, per D-CFG).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from halo_harness.config.paths import bridge_home

DEFAULT_THEME = "claude-dark"

_BASE_THEMES = ("claude-dark", "claude-light")
_SUFFIXES = ("", "-daltonized", "-ansi")

# Every valid theme name: the two base themes, plus each with a "-daltonized"
# or "-ansi" suffix (plan D-TUI: "claude-dark/claude-light (+ daltonized,
# ansi variants)"; help capture's own shorthand for the family is
# "claude-dark", "claude-light", "*-daltonized", "*-ansi").
VALID_THEMES = frozenset(f"{base}{suffix}" for base in _BASE_THEMES for suffix in _SUFFIXES)

# HALO_THEME is the 2.0.0 canonical name; CLAUDE_BRIDGE_THEME (claude-bridge
# era) and ROLO_CLAUDE_THEME (rolo-claude era) both still work, checked in
# this order -- only the ROLO_CLAUDE_* one matches the rename brief's own
# "BRIDGE_*/ROLO_CLAUDE_* still honoured, one DEBUG line" contract (see
# _DEPRECATED_ENV_VARS); CLAUDE_BRIDGE_THEME is an older, separate shim this
# release leaves exactly as it already behaved.
_ENV_VARS = ("HALO_THEME", "CLAUDE_BRIDGE_THEME", "ROLO_CLAUDE_THEME")
_DEPRECATED_ENV_VARS = ("ROLO_CLAUDE_THEME",)


def is_valid_theme(name: Optional[str]) -> bool:
    return isinstance(name, str) and name in VALID_THEMES


def _config_path() -> Path:
    return bridge_home() / "config.json"


def load_config() -> dict:
    """Read `~/.halo/config.json` fresh every call; `{}` if
    missing/invalid. Never raises -- this is a small local file a test may
    write then immediately re-read within the same process."""
    path = _config_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_persisted_theme() -> Optional[str]:
    """The theme name last written by `/theme`, or None if never set /
    unreadable."""
    theme = load_config().get("theme")
    return theme if isinstance(theme, str) else None


def set_config_value(key: str, value) -> Path:
    """Write `data[key] = value` into `~/.halo/config.json` (tmp +
    `os.replace`, preserving every other key already there) -- the generic
    form `persist_theme` and `halo config set` both build on. Never
    touches Claude Code's own settings.json.

    H10 Part B: `key` may be dotted (`"improve.model"`, `"improve.
    hint_threshold.repairs"`) to set a NESTED value -- `halo config
    set improve.model or:...` -- without disturbing any sibling key already
    under `improve`. A plain (undotted) key, every pre-H10 call site
    (`theme`, `compactionModel`, ...), is unchanged: `data[key] = value`
    exactly as before."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_config()
    if "." in key:
        parts = key.split(".")
        cursor = data
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[parts[-1]] = value
    else:
        data[key] = value
    tmp_path = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)
    return path


_MISSING = object()


def get_config_value(key: str, default=_MISSING):
    """The dotted-path read counterpart to `set_config_value` -- `key` may
    be `"improve.model"`; returns `default` (or raises `KeyError` if
    `default` is left unset) when any segment is missing."""
    data = load_config()
    cursor = data
    for part in key.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            if default is _MISSING:
                raise KeyError(key)
            return default
        cursor = cursor[part]
    return cursor


def persist_theme(name: str) -> Path:
    """Write `name` into `~/.halo/config.json`'s `"theme"` key,
    matching D-TUI's "`/theme` persists to `~/.claude-bridge/config.json`
    [now `~/.halo`], never Claude's settings". Returns the path
    written. Raises ValueError for a name outside `VALID_THEMES` -- callers
    (the `/theme` command, tests) are expected to validate user input
    themselves and surface a friendly message rather than let this raise."""
    if not is_valid_theme(name):
        raise ValueError(f"not a valid theme name: {name!r} (expected one of {sorted(VALID_THEMES)})")
    return set_config_value("theme", name)


def supports_truecolor(env: Optional[dict] = None) -> bool:
    """U5 scope D ("`system`/`ansi` theme auto-select ... `COLORTERM`,
    `TERM` checks"): best-effort truecolor detection from the terminal's
    own env vars, matching OpenCode's `system` theme idea ("auto-select
    the -ansi variant under tmux / no COLORTERM") without needing a live
    terminal query. True when `COLORTERM` is `truecolor`/`24bit`, OR
    `TERM`/`TERM_PROGRAM` names a terminal known to support 24-bit color
    even without COLORTERM set (kitty, iTerm, wezterm, vscode); False for
    everything else, including a bare `xterm`/`screen`/`tmux`/`linux` TERM
    or no TERM at all -- a conservative default (downgrade to -ansi) is
    the safer failure mode than assuming truecolor and rendering
    unreadably on a terminal that can't do it."""
    environ = env if env is not None else os.environ
    colorterm = (environ.get("COLORTERM") or "").strip().lower()
    if colorterm in ("truecolor", "24bit"):
        return True
    term_program = (environ.get("TERM_PROGRAM") or "").strip().lower()
    if term_program in ("iterm.app", "wezterm", "vscode", "ghostty"):
        return True
    term = (environ.get("TERM") or "").strip().lower()
    if "kitty" in term or "wezterm" in term or "ghostty" in term:
        return True
    return False


def auto_theme_for_env(env: Optional[dict] = None) -> str:
    """The theme `resolve_theme` falls back to when NOTHING (no `--theme`,
    no env var, no settings, no persisted choice) says otherwise: the
    plain default on a truecolor-capable terminal, its `-ansi` sibling
    everywhere else (a bare xterm, tmux/screen without COLORTERM passed
    through, a dumb/unknown TERM, ...)."""
    return DEFAULT_THEME if supports_truecolor(env) else f"{DEFAULT_THEME}-ansi"


def resolve_theme(*, cli_theme: Optional[str] = None, env: Optional[dict] = None,
                   settings_theme: Optional[str] = None, persisted_theme: Optional[str] = None) -> str:
    """Resolve the active theme name per the precedence order. `env`
    defaults to `os.environ` (a test seam accepts a plain dict instead); an
    unrecognized value at any tier is skipped (falls through to the next
    tier) rather than raising -- a stale/typo'd env var or settings value
    must never crash startup. `persisted_theme` (what `/theme` last wrote)
    sits between settings and the built-in default, matching D-TUI: a
    persisted choice from a previous session is itself a form of "settings",
    just ours rather than Claude Code's. Only when NONE of the four tiers
    named anything valid does the terminal's own truecolor support pick
    the final fallback (`auto_theme_for_env`) -- an explicit choice at any
    tier is always honoured verbatim, never silently upgraded/downgraded
    to an -ansi sibling."""
    environ = env if env is not None else os.environ

    if is_valid_theme(cli_theme):
        return cli_theme

    for var in _ENV_VARS:
        candidate = environ.get(var)
        if is_valid_theme(candidate):
            if var in _DEPRECATED_ENV_VARS:
                logging.getLogger(__name__).debug("%s is deprecated, use HALO_THEME instead", var)
            return candidate

    if is_valid_theme(settings_theme):
        return settings_theme

    if is_valid_theme(persisted_theme):
        return persisted_theme

    return auto_theme_for_env(environ)

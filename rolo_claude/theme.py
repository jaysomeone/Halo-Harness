"""rolo_claude.theme -- theme name resolution + persistence (U0 scope D).

Precedence: `--theme` (CLI flag) > `CLAUDE_BRIDGE_THEME`/`ROLO_CLAUDE_THEME`
(env, checked in that order) > settings `theme` > "claude-dark" (built-in
default). Pure data -- Textual itself is NOT imported here (U2's job); this
module only decides WHICH theme name is active and reads/writes the one
place a `/theme` command would persist a user's choice:
`~/.rolo-claude/config.json`'s `"theme"` key (never Claude Code's own
settings.json -- that file is config we only ever READ, per D-CFG).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from rolo_claude.config.paths import bridge_home

DEFAULT_THEME = "claude-dark"

_BASE_THEMES = ("claude-dark", "claude-light")
_SUFFIXES = ("", "-daltonized", "-ansi")

# Every valid theme name: the two base themes, plus each with a "-daltonized"
# or "-ansi" suffix (plan D-TUI: "claude-dark/claude-light (+ daltonized,
# ansi variants)"; help capture's own shorthand for the family is
# "claude-dark", "claude-light", "*-daltonized", "*-ansi").
VALID_THEMES = frozenset(f"{base}{suffix}" for base in _BASE_THEMES for suffix in _SUFFIXES)

_ENV_VARS = ("CLAUDE_BRIDGE_THEME", "ROLO_CLAUDE_THEME")


def is_valid_theme(name: Optional[str]) -> bool:
    return isinstance(name, str) and name in VALID_THEMES


def _config_path() -> Path:
    return bridge_home() / "config.json"


def load_config() -> dict:
    """Read `~/.rolo-claude/config.json` fresh every call; `{}` if
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
    """Write `data[key] = value` into `~/.rolo-claude/config.json` (tmp +
    `os.replace`, preserving every other key already there) -- the generic
    form `persist_theme` and `rolo-claude config set` both build on. Never
    touches Claude Code's own settings.json."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_config()
    data[key] = value
    tmp_path = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def persist_theme(name: str) -> Path:
    """Write `name` into `~/.rolo-claude/config.json`'s `"theme"` key,
    matching D-TUI's "`/theme` persists to `~/.claude-bridge/config.json`
    [now `~/.rolo-claude`], never Claude's settings". Returns the path
    written. Raises ValueError for a name outside `VALID_THEMES` -- callers
    (the `/theme` command, tests) are expected to validate user input
    themselves and surface a friendly message rather than let this raise."""
    if not is_valid_theme(name):
        raise ValueError(f"not a valid theme name: {name!r} (expected one of {sorted(VALID_THEMES)})")
    return set_config_value("theme", name)


def resolve_theme(*, cli_theme: Optional[str] = None, env: Optional[dict] = None,
                   settings_theme: Optional[str] = None, persisted_theme: Optional[str] = None) -> str:
    """Resolve the active theme name per the precedence order. `env`
    defaults to `os.environ` (a test seam accepts a plain dict instead); an
    unrecognized value at any tier is skipped (falls through to the next
    tier) rather than raising -- a stale/typo'd env var or settings value
    must never crash startup. `persisted_theme` (what `/theme` last wrote)
    sits between settings and the built-in default, matching D-TUI: a
    persisted choice from a previous session is itself a form of "settings",
    just ours rather than Claude Code's."""
    environ = env if env is not None else os.environ

    if is_valid_theme(cli_theme):
        return cli_theme

    for var in _ENV_VARS:
        candidate = environ.get(var)
        if is_valid_theme(candidate):
            return candidate

    if is_valid_theme(settings_theme):
        return settings_theme

    if is_valid_theme(persisted_theme):
        return persisted_theme

    return DEFAULT_THEME

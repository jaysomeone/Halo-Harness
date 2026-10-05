"""halo_harness.providers.codex_settings -- Halo 2.0.3 round 5i part 2:
reads (never writes) Codex CLI's own `config.toml` and `AGENTS.md` chain,
per docs/harness/CODEX-RESEARCH.md sections 5 and 8. Paired with
`providers.settings_merge`, which folds this beside Claude Code's own
settings/CLAUDE.md chain into one view for `halo doctor`/`/settings`/the
init wizard's "Settings sources" step.

`config.toml` is read with a DELIBERATELY PARTIAL hand-rolled parser --
this project ships no TOML dependency (`pyproject.toml`'s only runtime deps
are textual/rich/mcp) and this reader only ever needs to DISPLAY a handful
of documented keys, never to validate or round-trip the file. It handles
plain `key = value` scalars (string/bool/int/float/array-of-strings) and
`[table]`/`[table.sub]` headers; it does NOT handle multi-line arrays,
inline tables, or TOML's full string-escaping rules. A line it can't parse
is skipped, never raised -- `halo doctor`'s own line says what WAS found,
which is the honest contract for a read-only, best-effort display."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

_SECTION_RE = re.compile(r"^\[(?P<name>[A-Za-z0-9_.\-]+)\]\s*$")
_KV_RE = re.compile(r"^(?P<key>[A-Za-z0-9_.\-]+)\s*=\s*(?P<value>.+)$")

# docs/harness/CODEX-RESEARCH.md section 8: the project doc fallback names
# plus the default byte cap -- a `config.toml` override of either is read
# back from the parsed dict itself (`_project_doc_settings`), never
# hardcoded past the documented default.
_DEFAULT_MAX_BYTES = 32 * 1024
_DEFAULT_FALLBACK_NAMES: "list[str]" = []


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override)
    return Path.home() / ".codex"


def _parse_toml_value(raw: str):
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1]
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_toml_value(part) for part in inner.split(",") if part.strip()]
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def parse_simple_toml(text: str) -> dict:
    """Best-effort TOML -> nested dict (see module docstring for exactly
    what this does and does not handle). Comments (`#`, only when not
    inside a quoted value on that same line) and blank lines are skipped."""
    root: dict = {}
    current = root
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line and not ('"' in line or "'" in line):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
        section = _SECTION_RE.match(line)
        if section:
            current = root
            for part in section.group("name").split("."):
                current = current.setdefault(part, {})
            continue
        kv = _KV_RE.match(line)
        if kv:
            key_parts = kv.group("key").split(".")
            target = current
            for part in key_parts[:-1]:
                target = target.setdefault(part, {})
            try:
                target[key_parts[-1]] = _parse_toml_value(kv.group("value"))
            except Exception:
                continue
    return root


def load_codex_config(home: Optional[Path] = None) -> dict:
    """`{}` when `config.toml` is missing/unparseable/unreadable -- never
    raises. `home=None` uses `codex_home()`."""
    path = (home or codex_home()) / "config.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_simple_toml(text)


def load_project_codex_config(project_root: Path) -> dict:
    """`.codex/config.toml` project scoping (CODEX-RESEARCH.md section 5)
    -- `{}` when absent/unparseable. Halo never writes to this file."""
    path = Path(project_root) / ".codex" / "config.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_simple_toml(text)


# ---- AGENTS.md chain (CODEX-RESEARCH.md section 8 -- deliberately NOT the
# SAME walk `config.claude_md.discover_instructions` already does for
# Claude Code's own CLAUDE.md/AGENTS.md: Codex's rule is git-root-to-cwd,
# Halo's existing one is filesystem-root-to-cwd; the two genuinely differ,
# so this is its own, separate implementation.) ----------------------------

def _find_git_root(cwd: Path) -> Path:
    """Walks upward looking for a `.git` entry (a directory for an ordinary
    clone, a FILE for a worktree) -- no `git` subprocess at all, so this
    works even when git itself isn't installed. Falls back to `cwd` itself
    (Codex's own `--skip-git-repo-check` confirms it can run outside a repo
    at all; the walk then degenerates to just that one directory)."""
    current = Path(cwd).resolve()
    for directory in (current, *current.parents):
        if (directory / ".git").exists():
            return directory
    return current


def _read_nonempty(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text if text.strip() else None


def _project_doc_settings(config: dict) -> "tuple[int, list]":
    max_bytes = config.get("project_doc_max_bytes", _DEFAULT_MAX_BYTES)
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        max_bytes = _DEFAULT_MAX_BYTES
    names = config.get("project_doc_fallback_filenames", _DEFAULT_FALLBACK_NAMES)
    return max_bytes, (names if isinstance(names, list) else [])


def load_codex_agents_md_chain(cwd: Path, *, home: Optional[Path] = None) -> "list[dict]":
    """`[{"path": Path, "text": str}, ...]` in MERGE order (root-to-leaf,
    global first) -- concatenating every entry's `text` with blank lines
    reproduces Codex's own documented prompt exactly (closer-to-cwd text
    comes LAST, so it reads as the override). Stops accumulating once the
    combined total would exceed `project_doc_max_bytes` (default 32 KiB);
    a file that would push the chain over the cap is dropped entirely
    (never truncated mid-file), matching "combined files stop
    accumulating" in the confirmed research. Empty files are skipped."""
    home = home or codex_home()
    config = load_codex_config(home)
    max_bytes, fallback_names = _project_doc_settings(config)
    chain: "list[dict]" = []
    total = 0

    def _try_add(path: Path) -> bool:
        nonlocal total
        text = _read_nonempty(path)
        if text is None:
            return False
        if total + len(text.encode("utf-8")) > max_bytes:
            return False
        chain.append({"path": path, "text": text})
        total += len(text.encode("utf-8"))
        return True

    # 1. Global: AGENTS.override.md else AGENTS.md under CODEX_HOME.
    if not _try_add(home / "AGENTS.override.md"):
        _try_add(home / "AGENTS.md")

    # 2. Project: git root down to cwd, override-or-plain-or-fallback per dir.
    root = _find_git_root(cwd)
    current = Path(cwd).resolve()
    ancestors = [current, *current.parents]
    directories = [d for d in ancestors if d == root or root in d.parents]
    directories.reverse()  # root-to-leaf merge order
    for directory in directories:
        if _try_add(directory / "AGENTS.override.md"):
            continue
        if _try_add(directory / "AGENTS.md"):
            continue
        for name in fallback_names:
            if _try_add(directory / name):
                break
    return chain


def render_codex_agents_md_chain(chain: "list[dict]") -> str:
    return "\n\n".join(entry["text"].strip() for entry in chain if entry.get("text", "").strip())

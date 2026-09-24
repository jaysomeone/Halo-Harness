from __future__ import annotations
import os
import re
import hashlib
import shutil
from pathlib import Path


def home() -> Path:
    """Return BRIDGE_TEST_HOME env var if set, else Path.home()."""
    if "BRIDGE_TEST_HOME" in os.environ:
        return Path(os.environ["BRIDGE_TEST_HOME"])
    return Path.home()


def claude_config_dir() -> Path:
    """Return CLAUDE_CONFIG_DIR env var if absolute, else home()/".claude"."""
    if "CLAUDE_CONFIG_DIR" in os.environ:
        p = Path(os.environ["CLAUDE_CONFIG_DIR"])
        if p.is_absolute():
            return p
    return home() / ".claude"


def claude_json_path() -> Path:
    """`$CLAUDE_CONFIG_DIR/.claude.json` when CLAUDE_CONFIG_DIR is set (a
    legacy `<configDir>/.config.json` wins if present there), else
    home()/".claude.json" [bin sec.8]."""
    if "CLAUDE_CONFIG_DIR" in os.environ:
        cfg_dir = claude_config_dir()
        legacy = cfg_dir / ".config.json"
        if legacy.exists():
            return legacy
        return cfg_dir / ".claude.json"
    return home() / ".claude.json"


def bridge_home() -> Path:
    """Return BRIDGE_STATE_DIR env var if set, else home()/".rolo-claude"."""
    if "BRIDGE_STATE_DIR" in os.environ:
        return Path(os.environ["BRIDGE_STATE_DIR"])
    return home() / ".rolo-claude"


def managed_dir() -> Path:
    """Return per-OS managed-settings directory (best effort, may not
    exist). Test seam: BRIDGE_TEST_MANAGED_DIR overrides everything below
    (finding 14) -- without it, settings/CLAUDE.md tests would read the
    REAL `C:\\Program Files\\ClaudeCode` or `/etc/claude-code` on whatever
    machine runs them."""
    if "BRIDGE_TEST_MANAGED_DIR" in os.environ:
        return Path(os.environ["BRIDGE_TEST_MANAGED_DIR"])
    if os.name == "nt":
        return Path("C:\\Program Files\\ClaudeCode")
    # POSIX
    # Try /etc/claude-code first
    etc = Path("/etc/claude-code")
    if etc.exists():
        return etc
    # Then /Library/Application Support/ClaudeCode on macOS
    return Path("/Library/Application Support/ClaudeCode")


def managed_settings_files() -> list[Path]:
    """Return existing managed settings files in precedence order."""
    base = managed_dir()
    files = []
    # managed_dir()/"managed-settings.json"
    main = base / "managed-settings.json"
    if main.exists():
        files.append(main)
    # Every *.json under managed_dir()/"managed-settings.d/"
    d = base / "managed-settings.d"
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            if f.is_file():
                files.append(f)
    return files


def _java_string_hash(s: str) -> int:
    """Java/JS `String.hashCode()`: h = h*31 + charCode, wrapped to a signed
    32-bit int, over UTF-16 CODE UNITS (so an astral character is split into
    a surrogate pair first, matching JS's `charCodeAt`, not Python's
    code-point iteration) [bin sec.12: `(h<<5)-h+charCode|0`]."""
    h = 0
    for ch in s:
        code = ord(ch)
        units = (code,)
        if code > 0xFFFF:
            v = code - 0x10000
            units = (0xD800 + (v >> 10), 0xDC00 + (v & 0x3FF))
        for u in units:
            h = (h * 31 + u) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return h


def _base36(n: int) -> str:
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


def project_slug(cwd: str | Path) -> str:
    """Convert cwd to a filesystem-safe slug: every non-alphanumeric char ->
    '-'; slugs over 200 chars are truncated to 200 and get a base-36
    `abs(javaHash(originalPath))` suffix (over the UNTRUNCATED original
    string, not the slug) [bin sec.12] -- NOT a sha1 hash."""
    cwd_str = str(cwd)
    slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd_str)
    if len(slug) <= 200:
        return slug
    truncated = slug[:200]
    suffix = _base36(abs(_java_string_hash(cwd_str)))
    return f"{truncated}-{suffix}"


_WIN_DRIVE_RE = re.compile(r"^([a-zA-Z]):[\\/](.*)$", re.DOTALL)


def normalize_cwd(path: str | Path) -> str:
    """Return an absolute path with forward slashes and an uppercase drive
    letter. `~/.claude.json`'s `projects` keys can be Windows-shaped
    (`C:\\Users\\rolo`) regardless of which OS is running THIS process --
    rolo-claude's primary target is Linux, but it must still recognize a
    project key copied from a Windows Claude Code install. A Windows-drive-
    shaped INPUT is therefore detected by pattern (never by host `os.name`)
    and normalized without ever calling `Path.absolute()`, which on POSIX
    treats "C:/Users/user" as a relative path and prepends the real cwd
    (producing garbage like "/home/user/proj/C:/Users/user"). Anything that
    doesn't match the drive-letter shape falls through to ordinary
    host-appropriate absolute-path normalization."""
    s = str(path)
    m = _WIN_DRIVE_RE.match(s)
    if m:
        drive, rest = m.groups()
        return f"{drive.upper()}:/{rest.replace(chr(92), '/')}"
    p = Path(s)
    if not p.is_absolute():
        p = Path.cwd() / p
    normalized = str(p)
    if os.name == "nt":
        normalized = normalized.replace("\\", "/")
        if re.match(r"^[a-zA-Z]:/", normalized):
            normalized = normalized[0].upper() + normalized[1:]
    return normalized


def project_key_candidates(cwd: str | Path) -> list[str]:
    """Return both key forms Claude Code's projects map might use for this cwd."""
    normalized = normalize_cwd(cwd)
    candidates = [normalized]
    # Second form: replace '/' with '\' (on POSIX this is identical)
    candidates.append(normalized.replace("/", "\\"))
    return candidates


def lookup_project(claude_json: dict, cwd: str | Path) -> dict:
    """Merge every projects[key] record matching project_key_candidates(cwd)."""
    # Degrade gracefully on malformed claude_json
    if not isinstance(claude_json, dict):
        return {}
    projects = claude_json.get("projects")
    if not isinstance(projects, dict):
        return {}
    candidates = project_key_candidates(cwd)
    matching_records = []
    for key, value in projects.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        normalized_key = normalize_cwd(key)
        if normalized_key in candidates:
            matching_records.append(value)
    if not matching_records:
        return {}
    # Merge records
    result: dict = {}
    # Track seen values for list dedup
    list_values: dict[str, list] = {}
    seen_list_items: dict[str, set] = {}
    for rec in matching_records:
        for k, v in rec.items():
            if v is None:
                if k not in result:
                    result[k] = None
                continue
            if isinstance(v, bool):
                if k == "hasTrustDialogAccepted":
                    result[k] = result.get(k, False) or v
                else:
                    # For other booleans, OR them
                    result[k] = result.get(k, False) or v
            elif isinstance(v, list):
                if k not in list_values:
                    list_values[k] = []
                    seen_list_items[k] = set()
                for item in v:
                    # Dedup by value equality (for hashable items)
                    try:
                        if item not in seen_list_items[k]:
                            list_values[k].append(item)
                            seen_list_items[k].add(item)
                    except TypeError:
                        # Unhashable item, just append
                        list_values[k].append(item)
                result[k] = list_values[k]
            elif isinstance(v, dict):
                if k not in result:
                    result[k] = {}
                if isinstance(result[k], dict):
                    # Shallow merge, later record wins key collision
                    result[k].update(v)
                else:
                    # Previous non-dict value, replace with dict
                    result[k] = v.copy()
            else:
                # First non-null wins for other types
                if k not in result:
                    result[k] = v
    return result


def plans_dir() -> Path:
    """Return claude_config_dir()/"plans"."""
    return claude_config_dir() / "plans"


def find_git_root(path: str | Path) -> Path | None:
    """Walk up from `path` looking for a `.git` entry (dir or file, for a
    worktree/submodule pointer); returns the containing directory, or None
    if `path` isn't inside a git working tree. Pure filesystem check -- no
    `git` subprocess, so it works even when `git` isn't on PATH."""
    current = Path(path).resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def memory_dir(cwd: str | Path, settings=None) -> Path:
    """Return the memory directory for cwd, honoring an explicit
    autoMemoryDirectory override from `settings` if one is set. `settings`
    is duck-typed on purpose (this is a leaf module with no dependency on
    config/settings.py's Settings class): a Settings-like OBJECT exposes it
    as the snake_case attribute `.auto_memory_directory`, while a plain dict
    (e.g. in a simple test) would use the raw JSON key `autoMemoryDirectory`
    -- both are tried (Settings itself is responsible for only ever
    surfacing a value sourced from policy/flag/local/user, never project
    [bin sec.12/finding 2] -- this leaf function just consumes whatever it's
    given and `expanduser()`s it). Absent an override, the slug is built
    from the git worktree root when `cwd` is inside one, else `cwd` itself
    [bin sec.12: "git root or cwd", finding 2] -- resolved to an absolute
    path once so a relative `--cwd .` can't collapse the slug to "-"."""
    auto_dir = None
    if settings is not None:
        auto_dir = getattr(settings, "auto_memory_directory", None)
        if auto_dir is None and isinstance(settings, dict):
            auto_dir = settings.get("autoMemoryDirectory")
    if auto_dir and isinstance(auto_dir, str):
        return Path(auto_dir).expanduser()
    abs_cwd = Path(cwd).resolve()
    root = find_git_root(abs_cwd) or abs_cwd
    slug = project_slug(root)
    return claude_config_dir() / "projects" / slug / "memory"


def git_bash() -> Path | None:
    """Return git bash path if found, else None."""
    if os.name == "nt":
        # Windows: check env var first
        env_path = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
        if env_path:
            p = Path(env_path)
            if p.exists():
                return p
        # Try common locations
        paths = [
            Path("C:\\Program Files\\Git\\usr\\bin\\bash.exe"),
            Path("C:\\Program Files\\Git\\bin\\bash.exe"),
        ]
        for p in paths:
            if p.exists():
                return p
        return None
    # POSIX
    bash = shutil.which("bash")
    if bash:
        return Path(bash)
    return Path("/bin/bash")


def to_posix(path_str: str) -> str:
    """Convert a Windows-shaped path string to git-bash/posix style (drive
    letter `C:/` or `C:\\` -> `/c/`). Based on the INPUT string's own shape,
    not the current OS, so this is safe to call unconditionally."""
    posix = path_str.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", posix)
    if m:
        drive, rest = m.groups()
        return f"/{drive.lower()}/{rest}"
    return posix


def from_posix(path_str: str) -> str:
    """Best-effort inverse of to_posix: a leading `/x/` becomes `X:/`;
    anything else (already a native path, or a plain relative posix path,
    which is valid on Windows with forward slashes too) is returned as-is."""
    m = re.match(r"^/([a-zA-Z])/(.*)$", path_str)
    if m:
        drive, rest = m.groups()
        return f"{drive.upper()}:/{rest}"
    return path_str

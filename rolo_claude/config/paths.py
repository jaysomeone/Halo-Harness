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
    """Return home()/".claude.json"."""
    return home() / ".claude.json"


def bridge_home() -> Path:
    """Return BRIDGE_STATE_DIR env var if set, else home()/".rolo-claude"."""
    if "BRIDGE_STATE_DIR" in os.environ:
        return Path(os.environ["BRIDGE_STATE_DIR"])
    return home() / ".rolo-claude"


def managed_dir() -> Path:
    """Return per-OS managed-settings directory (best effort, may not exist)."""
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


def project_slug(cwd: str | Path) -> str:
    """Convert cwd to a filesystem-safe slug, truncated and hashed if >200 chars."""
    cwd_str = str(cwd)
    # Replace every non-alphanumeric char with '-'
    slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd_str)
    if len(slug) <= 200:
        return slug
    # Truncate to 200 chars and append hash suffix
    truncated = slug[:200]
    suffix = hashlib.sha1(slug.encode()).hexdigest()[:8]
    return f"{truncated}-{suffix}"


def normalize_cwd(path: str | Path) -> str:
    """Return absolute path with forward slashes and uppercase drive letter."""
    p = Path(path).absolute()
    # Convert to forward slashes
    normalized = str(p).replace("\\", "/")
    # Uppercase drive letter if pattern matches ^[a-zA-Z]:/
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


def memory_dir(cwd: str | Path, settings=None) -> Path:
    """Return the memory directory for cwd, honoring an explicit
    autoMemoryDirectory override from `settings` if one is set. `settings`
    is duck-typed on purpose (this is a leaf module with no dependency on
    config/settings.py's Settings class): a Settings-like OBJECT exposes it
    as the snake_case attribute `.auto_memory_directory`, while a plain dict
    (e.g. in a simple test) would use the raw JSON key `autoMemoryDirectory`
    -- both are tried."""
    auto_dir = None
    if settings is not None:
        auto_dir = getattr(settings, "auto_memory_directory", None)
        if auto_dir is None and isinstance(settings, dict):
            auto_dir = settings.get("autoMemoryDirectory")
    if auto_dir and isinstance(auto_dir, str):
        return Path(auto_dir)
    slug = project_slug(cwd)
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

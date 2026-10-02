from __future__ import annotations
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

# 2.0.0 rename: the env-var compatibility shim every HALO_* knob goes
# through -- a legacy BRIDGE_*/ROLO_CLAUDE_* name keeps working forever
# (never a hard cutover), logged once per lookup at DEBUG so a box still
# running on the old names is easy to spot in --debug output without
# printing anything to stderr on every ordinary run. Deliberately excludes
# the handful of test-only seams (BRIDGE_TEST_*, BRIDGE_STATE_DIR,
# BRIDGE_ENV_FILE is NOT excluded -- see env_file_path below) that keep
# their bare names forever instead of growing a HALO_ twin.
_LEGACY_ENV_PREFIXES = ("BRIDGE_", "ROLO_CLAUDE_")

# finding 11 (2.0.0 fixpass): CHANGELOG.md item 4 and docs/CONFIG.md:199-200
# both promise ONE debug line per deprecated name, but `PING_INTERVAL` alone
# is looked up per proxied request AND per turn -- without this, a box still
# running on a legacy name floods --debug output with a repeat of the same
# line forever instead of ever actually helping anyone spot it. Keyed on the
# OLD key name (not name/old_key pair -- a name is only ever reached through
# one legacy prefix per call, the first one present), process-wide, never
# reset: a second `halo` invocation gets its own fresh module import and so
# its own fresh warning, exactly once, same as before this finding.
_deprecated_names_warned: "set[str]" = set()


def env_compat(name: str, env: Optional[dict] = None, default: Optional[str] = None) -> Optional[str]:
    """Resolve a config knob as `HALO_<name>` first, falling back to the
    legacy `BRIDGE_<name>` then `ROLO_CLAUDE_<name>` (checked in that
    order) -- exactly the precedence the 2.0.0 Names table promises ("new
    HALO_* names ... BRIDGE_* and ROLO_CLAUDE_* still honoured"). `env`
    defaults to `os.environ`; pass a plain dict (e.g. the `env` parameter
    several `providers/config.py` resolvers already take for testability)
    to resolve against something else without touching the real process
    environment. Presence-based, like `dict.get` -- a legacy name set to
    the empty string still wins over a default, same as the new name
    would."""
    source = env if env is not None else os.environ
    new_key = f"HALO_{name}"
    if new_key in source:
        return source[new_key]
    for prefix in _LEGACY_ENV_PREFIXES:
        old_key = f"{prefix}{name}"
        if old_key in source:
            if old_key not in _deprecated_names_warned:
                _deprecated_names_warned.add(old_key)
                logging.getLogger(__name__).debug(
                    "%s is deprecated, use %s instead", old_key, new_key)
            return source[old_key]
    return default


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


# 2.0.0 fixpass finding 1: the AUTO-migration decision (never the explicit
# BRIDGE_STATE_DIR override just above, which always stays fully dynamic)
# is memoized per resolved `home()` value, so one process can never switch
# state dirs mid-run (a transient rename failure's `old_dir` fallback could
# otherwise flip to `new_dir` later just because a retry would now
# succeed). Keyed on home() rather than a single bare slot: production has
# exactly one home() for its whole lifetime (so this behaves like a plain
# one-shot cache there, which is the point), while a test suite's many
# BRIDGE_TEST_HOME-scoped calls in ONE process each get their own
# independent first-call resolution, same as if each ran in its own
# process.
_state_dir_memo: "dict[str, Path]" = {}


def _same_dir(a: Path, b: Path) -> bool:
    """True iff `a` and `b` name the same real directory, including when
    `a` is a symlink/junction pointing at `b` (`os.path.samefile` resolves
    a Windows junction the same way it resolves a POSIX symlink). False,
    never raises, when either is missing or inaccessible."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def state_dir_both_exist_nonempty() -> "Optional[tuple[Path, Path]]":
    """`(old_dir, new_dir)` when BOTH exist as genuinely distinct
    directories -- never when `old_dir` happens to resolve to the exact
    same real directory as `new_dir` (e.g. a manually-created symlink; a
    plain migration never creates one -- see `bridge_home()`'s own
    docstring, 2.0.0 fixpass item B) -- and `old_dir` still has something
    in it. `None` otherwise, including whenever
    BRIDGE_STATE_DIR overrides the whole question. Read-only, never
    raises: both `bridge_home()` (one warning per process, see below) and
    `doctor` (a standing WARN line, 2.0.0 rename brief item 1: "both-exist
    case is silent too") share this ONE detection so they can never
    disagree about it."""
    if "BRIDGE_STATE_DIR" in os.environ:
        return None
    home_dir = home()
    new_dir = home_dir / ".halo"
    old_dir = home_dir / ".rolo-claude"
    if not (new_dir.is_dir() and old_dir.exists()):
        return None
    if _same_dir(old_dir, new_dir):
        return None
    try:
        has_content = any(old_dir.iterdir())
    except OSError:
        return None
    return (old_dir, new_dir) if has_content else None


def bridge_home() -> Path:
    """Return BRIDGE_STATE_DIR env var if set (a test seam that keeps its
    bare name forever, never a HALO_STATE_DIR twin -- see env_compat's own
    docstring), else home()/".halo" -- migrating an existing home()/
    ".rolo-claude" there first, on whichever call happens to be the first
    in this process to notice it's missing (2.0.0 rename brief item 2):
    renamed (never copied, so no stale duplicate is ever left behind),
    announced with exactly one stderr line.

    2.0.0 fixpass item B: NO link is left at the old location (an earlier
    build briefly created one) -- `rm -rf ~/.rolo-claude/` with a trailing
    slash follows a symlink/junction and would empty `~/.halo` right along
    with it. A still-installed `rolo-claude` 1.0.1 must simply not be run
    again once this has migrated; it would start from a truly empty
    `~/.rolo-claude`.

    2.0.0 fixpass finding 1: a rename that fails (a locked file on
    Windows, a read-only/cross-device home on Linux) no longer silently
    abandons all state into a `new_dir` that doesn't exist yet -- it
    prints ONE warning naming both paths and returns `old_dir` instead, so
    this run keeps using the directory that's actually there (memoized,
    like every other outcome here, so a later retry within the SAME
    process can't switch directories out from under it mid-run -- only a
    fresh process gets to try the rename again). When both directories
    already exist non-trivially (the rename half-succeeded on an earlier
    run, or `old_dir` was recreated since), `new_dir` wins and a single
    warning fires the first time this is noticed.

    2.0.0 fixpass item D: when the rename itself raises but a CONCURRENT
    process has already finished migrating (`new_dir` now exists and
    `old_dir` is now gone), that `new_dir` is adopted silently instead of
    printing a false "could not migrate" warning -- the rename didn't fail
    because of a real obstacle, it failed because there was nothing left
    to rename."""
    if "BRIDGE_STATE_DIR" in os.environ:
        return Path(os.environ["BRIDGE_STATE_DIR"])
    home_dir = home()
    home_key = str(home_dir)
    memoized = _state_dir_memo.get(home_key)
    if memoized is not None:
        return memoized

    new_dir = home_dir / ".halo"
    old_dir = home_dir / ".rolo-claude"
    resolved = new_dir
    if not new_dir.exists() and old_dir.exists():
        try:
            old_dir.rename(new_dir)
        except OSError:
            if new_dir.exists() and not old_dir.exists():
                # A concurrent process migrated first while this rename
                # was in flight -- nothing actually went wrong.
                resolved = new_dir
            else:
                print(f"halo: warning: could not migrate state directory {old_dir} -> {new_dir} "
                      f"(will retry on a later run); using {old_dir} for this run", file=sys.stderr)
                resolved = old_dir
        else:
            print(f"halo: migrated state directory {old_dir} -> {new_dir}", file=sys.stderr)
    else:
        both = state_dir_both_exist_nonempty()
        if both is not None:
            print(f"halo: warning: both {both[0]} and {both[1]} exist; using {both[1]} "
                  f"(run `halo doctor` for details)", file=sys.stderr)
    _state_dir_memo[home_key] = resolved
    return resolved


def legacy_env_file_path() -> Path:
    """The pre-2.0.0 provider-credentials env file path, unconditionally
    (never consults HALO_ENV_FILE/BRIDGE_ENV_FILE -- that's `env_file_
    path()`'s job). A plain, side-effect-free path value; callers decide
    for themselves whether consulting it makes sense (an explicit override
    is total and should skip it entirely -- see `env_file_path_for_write`
    and `providers.config.load_provider_env_files`)."""
    return home() / ".config" / "vibes-hacker" / "env"


def env_file_path() -> Path:
    """Return the provider-credentials env file path to READ/display
    (2.0.0 fixpass finding 4): `HALO_ENV_FILE`, else legacy
    `BRIDGE_ENV_FILE`/`ROLO_CLAUDE_ENV_FILE` (one DEBUG line, same as every
    other env_compat call) if set; else the NEW canonical path
    (`~/.config/halo/env`), regardless of whether it exists yet. Pure --
    never touches the filesystem. This is no longer "either/or" with the
    legacy file (that was the bug: once the new file existed at all, a
    credential kept only in the legacy one silently vanished) -- a caller
    that needs to actually LOAD credentials calls `providers.config.
    load_provider_env_files()` instead, which reads this path THEN the
    legacy one with `setdefault`; a caller that needs to WRITE calls
    `env_file_path_for_write()`, which copies the legacy file forward
    first when only it exists."""
    override = env_compat("ENV_FILE")
    if override:
        return Path(override)
    return home() / ".config" / "halo" / "env"


def env_file_path_for_write() -> Path:
    """The path a WRITER (`halo init`, the tabbed provider setup) should
    open to add/update a credential (2.0.0 fixpass finding 4): same
    HALO_ENV_FILE/legacy override `env_file_path()` honors, else always
    the NEW canonical path -- `halo init` never resurrects the legacy
    directory. The FIRST time this is called with the new file still
    absent and a legacy file already present, the legacy file's content is
    copied forward (directory 0700, file 0600 on POSIX; best-effort chmod
    on Windows, which has no equivalent bit-for-bit mode) before returning,
    so whatever the writer appends lands next to every credential the user
    already had, instead of silently orphaning them in a file nothing will
    ever look at again once the new one exists.

    The legacy file itself is NEVER touched: other tools on the same box
    read `~/.config/vibes-hacker/env` too (it predates this harness as a
    shared credentials file). Instead the copied-forward new file starts
    with `ENV_IMPORT_MARKER`, and `load_provider_env_files()` skips the
    legacy file whenever the new file carries that marker -- so a key later
    DELETED from the new file can never come back from the legacy one."""
    override = env_compat("ENV_FILE")
    if override:
        return Path(override)
    new_path = home() / ".config" / "halo" / "env"
    if not new_path.exists():
        legacy = legacy_env_file_path()
        if legacy.exists():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(new_path.parent, 0o700)
            except OSError:
                pass
            content = legacy.read_text(encoding="utf-8-sig", errors="replace")
            # Atomic: a crash mid-write must never leave a marked, truncated
            # file behind (that would switch the legacy fallback off with
            # half the credentials missing).
            tmp_path = new_path.with_name(new_path.name + ".tmp")
            tmp_path.write_text(ENV_IMPORT_MARKER + "\n" + content, encoding="utf-8")
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, new_path)
    return new_path


ENV_IMPORT_MARKER = "# imported from the pre-2.0.0 env file (~/.config/vibes-hacker/env); halo reads only this file now"


def env_file_has_import_marker(path: Path) -> bool:
    """True when `path` is a new-style env file that was copied forward from
    the legacy one (any line equals `ENV_IMPORT_MARKER`; a comment added
    above it by hand or a byte-order mark from a Windows editor must not
    hide it), meaning the legacy file must no longer be consulted."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if line.strip() == ENV_IMPORT_MARKER:
                    return True
    except OSError:
        pass
    return False


def rewrite_legacy_state_dir_prefix(path_str: str) -> str:
    """2.0.0 fixpass finding 10: a RESUMED 1.0.1 session's own prior
    transcript can literally contain an absolute pointer under the OLD
    state dir ("Full output saved to .../.rolo-claude/sessions/.../
    tool-results/X.txt" -- tools/truncate.py and tools/mcp_tool.py's own
    spill-file message). Since `bridge_home()` leaves no link behind after
    migrating (item B), that old prefix is simply dead on any box that's
    migrated -- this rewrites it at read time instead: when `path_str`
    starts with the OLD state dir (`home()/.rolo-claude`) and that exact
    path is no longer a real, usable directory, the prefix is swapped for
    the NEW state dir -- the rename preserves every relative path
    underneath it, so the equivalent file, if it still exists at all, is
    at the exact same path under the new root. Returns `path_str`
    unchanged whenever the old dir is somehow still a real directory there
    (a box that hasn't migrated yet -- nothing to fix) or doesn't
    prefix-match at all. Pure string rewrite; never touches the filesystem
    beyond the one `is_dir()` check. Applied to every Read call by
    `PermissionEngine.decide()` itself (item C), not just this session's
    own tool-result spill files, so it fires in every permission mode."""
    home_dir = home()
    old_dir = home_dir / ".rolo-claude"
    old_prefix = str(old_dir)
    if not (path_str == old_prefix or path_str.startswith(old_prefix + os.sep)
            or path_str.startswith(old_prefix + "/")):
        return path_str
    if old_dir.is_dir():
        return path_str  # still there (a real leftover dir, or a working link) -- nothing to fix
    new_dir = home_dir / ".halo"
    return str(new_dir) + path_str[len(old_prefix):]


def background_net_disabled() -> bool:
    """Test seam (1.0.1 part 2 fixpass finding 10): `BRIDGE_TEST_NO_
    BACKGROUND_NET=1` tells every background catalog/balance-refresh
    worker (the TUI's launch-time catalog refresh, the OpenRouter balance
    worker, `/model`'s own open-time refresh, and `headless.build_session`'s
    session-start Databricks thread) to return immediately instead of
    touching the network -- set by `tests.helpers.provider_env_defaults.
    ensure_default_provider_credentials`, so mounting a BridgeApp or
    building a session in a test never races a real (or fake-credentialed)
    HTTP call against openrouter.ai/api.anthropic.com/a fake Databricks
    host just from being constructed (contradicting `headless.py`'s own
    documented contract that building a session never triggers first-time
    catalog discovery)."""
    return os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET") == "1"


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
    # macOS keeps managed settings under /Library; every other POSIX box
    # (Linux first) uses /etc/claude-code whether or not it exists yet --
    # seen live on the Kali VM: `halo mcp list` named the macOS path.
    if os.uname().sysname == "Darwin":
        return Path("/Library/Application Support/ClaudeCode")
    return etc


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
    halo's primary target is Linux, but it must still recognize a
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

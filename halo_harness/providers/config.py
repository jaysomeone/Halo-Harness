"""halo_harness.providers.config -- environment discovery, OpenRouter/
Databricks config resolution, the settings-env chain, logging setup, and a
handful of small pure helpers (jdumps/estimate_tokens/dump_debug). Moved out
of bridge.py unchanged in the H0 package split; see wip/SIGNATURES.md part1
("Constants / env") for the full contract. No behavior change from v0.2.1
except bridge_py_sha1(), which now hashes the whole bridge.py + providers
tree instead of just this one file (see its docstring).
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from halo_harness import __version__
# 2.0.0 rename: config/paths.py has zero halo_harness-internal imports of
# its own (stdlib only), so importing it here at module level carries no
# circularity risk -- env_compat/env_file_path are the single shared
# implementation every BRIDGE_*/ROLO_CLAUDE_*-reading call site below now
# goes through, so the HALO_* precedence + one-DEBUG-line-per-legacy-use
# behavior lives in exactly one place.
from halo_harness.config.paths import env_compat, env_file_path as _resolve_env_file_path, legacy_env_file_path

def home() -> Path:
    """Return BRIDGE_TEST_HOME if set, else user home directory."""
    if "BRIDGE_TEST_HOME" in os.environ:
        return Path(os.environ["BRIDGE_TEST_HOME"])
    return Path.home()


def default_state_dir() -> Path:
    """Return BRIDGE_STATE_DIR if set, else ~/.halo (Halo Harness data dir;
    shared with the harness's own session/MCP/trust state). Delegates to
    `config.paths.bridge_home()` (lazy import -- `providers/config.py`
    otherwise has no dependency on the rest of the package tree) so the
    2.0.0 one-time `~/.rolo-claude` -> `~/.halo` migration lives in exactly
    one place and fires identically whichever name a caller reaches it
    through (`halo`'s own TUI/print-mode path and `halo proxy`/bridge.py
    both resolve the SAME state dir, migrated at most once combined, not
    once per name)."""
    if "BRIDGE_STATE_DIR" in os.environ:
        return Path(os.environ["BRIDGE_STATE_DIR"])
    from halo_harness.config.paths import bridge_home
    return bridge_home()


def _parse_env_file(path: Path) -> dict[str, str]:
    """Pure KEY=value parse (finding 10) -- no `os.environ` side effect,
    so a caller that only needs to know WHICH KEYS an env file would
    inject (to strip them from a tool child's environment) never has to
    mutate the process environment just to find out."""
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    try:
        with open(path, encoding="utf-8-sig") as f:  # tolerate a BOM from a Windows editor
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Strip optional 'export ' prefix
                if line.startswith("export "):
                    line = line[7:].strip()
                # Parse KEY=value
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    # Remove surrounding quotes
                    if (value.startswith('"') and value.endswith('"')) or (
                        value.startswith("'") and value.endswith("'")
                    ):
                        value = value[1:-1]
                    if key:
                        loaded[key] = value
    except (OSError, UnicodeDecodeError):
        pass
    return loaded


def load_env_file(path: Path) -> dict[str, str]:
    """
    Load KEY=value lines from file, setdefault into os.environ, return loaded dict.
    """
    loaded = _parse_env_file(path)
    for key, value in loaded.items():
        os.environ.setdefault(key, value)
    return loaded


def _legacy_env_file_to_consult() -> "Optional[Path]":
    """The legacy env file a READER should ALSO check, or None when there
    is none to check -- an explicit HALO_ENV_FILE/BRIDGE_ENV_FILE override
    is total (same override `env_file_path()` itself honors) and skips the
    legacy file entirely, same as when it happens to equal the legacy path
    already (nothing left to add)."""
    if env_compat("ENV_FILE"):
        return None
    return legacy_env_file_path()


def load_provider_env_files() -> dict[str, str]:
    """2.0.0 fixpass finding 4: loads `env_file_path()` (the new canonical
    path, or the HALO_ENV_FILE/legacy-named override when one is set) THEN
    the legacy `~/.config/vibes-hacker/env` file -- `load_env_file`'s own
    `os.environ.setdefault` means the new file's keys always win on a
    collision, exactly like before this fix for every key the new file
    actually has. Every caller that used to do `load_env_file(env_file_
    path())` to load the provider-credentials file now calls this instead,
    so a `DATABRICKS_TOKEN` kept only in the legacy file is never silently
    invisible again just because the new file also exists (for some OTHER
    key). An explicit override collapses this to that one file alone."""
    new_path = _resolve_env_file_path()
    loaded = dict(load_env_file(new_path))
    legacy = _legacy_env_file_to_consult()
    # Item E: once the new file was copied forward from the legacy one it
    # carries ENV_IMPORT_MARKER, and the legacy file (which other tools on
    # the box still read, so it is never renamed or edited) is no longer
    # consulted -- a key deleted from the new file stays deleted.
    from halo_harness.config.paths import env_file_has_import_marker
    if legacy is not None and not env_file_has_import_marker(new_path):
        for key, value in load_env_file(legacy).items():
            loaded.setdefault(key, value)
    return loaded


# finding 10: the harness's OWN provider-credential env vars -- never
# forwarded to a Bash/PowerShell/MCP child, no matter which of the several
# ways (the env file, a real ambient env var, BRIDGE_DBX_* overrides, ...)
# they got into this process's environment. A routine `env`/`printenv`
# step inside a tool call must not write these into the transcript, the
# session JSONL, or the next upstream request.
_HARNESS_SECRET_ENV_KEYS = frozenset({
    "OPENROUTER_API_KEY", "DATABRICKS_TOKEN", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY", "BRIDGE_DBX_TOKEN", "HALO_DBX_TOKEN", "ROLO_CLAUDE_DBX_TOKEN",
    # N1 (1.0.1 final pass): OPENROUTER_MANAGEMENT_KEY is a SEPARATE,
    # higher-privilege OpenRouter credential (resolve_openrouter_management_
    # key, below) -- it must never reach a tool child either, same as the
    # ordinary OPENROUTER_API_KEY. TYPESAFE_API_KEY is the TypeSafe provider's
    # own credential (providers.enablement.credentials_present), added here
    # for the same reason. Both are already covered by cc_child_env (which
    # calls tool_child_env first) and by hooks.py's own env builder (which
    # calls tool_child_env directly) -- no separate list to update there.
    "OPENROUTER_MANAGEMENT_KEY", "TYPESAFE_API_KEY",
})


def tool_child_env(env: dict, *, env_file_path: Optional[Path] = None) -> dict:
    """`env` (typically `Settings.effective_env`, shell < settings chain)
    minus every key the harness's own env-file loader would inject PLUS
    the fixed secret-key list above -- the environment a Bash/PowerShell/
    MCP CHILD PROCESS should actually see. `env_file_path` defaults to the
    same `HALO_ENV_FILE`/legacy-`BRIDGE_ENV_FILE` (or `~/.config/halo/env`/
    legacy `~/.config/vibes-hacker/env`) location `resolve_config`/
    `load_env_file` already use (see `config.paths.env_file_path`), read
    PURELY (see `_parse_env_file` -- never touches `os.environ`)."""
    path = env_file_path or _resolve_env_file_path()
    strip_keys = set(_parse_env_file(path).keys()) | _HARNESS_SECRET_ENV_KEYS
    # 2.0.0 fixpass finding 4: a key sitting ONLY in the legacy file (the
    # new one exists too, for some other key, or doesn't exist at all) must
    # still never reach a tool child -- strip from BOTH files' keys, not
    # just whichever one `env_file_path()`/an explicit override resolved
    # to. Skipped outright when an explicit override is active, same as
    # `load_provider_env_files()`.
    if env_file_path is None:
        legacy = _legacy_env_file_to_consult()
        # Same gate as `load_provider_env_files()`: once the new file carries
        # the import marker the legacy file is out of the picture, so a key
        # the user removed from the new file and exports on purpose is not
        # stripped from tool children because the shared legacy file still
        # lists it.
        from halo_harness.config.paths import env_file_has_import_marker
        if legacy is not None and not env_file_has_import_marker(path):
            strip_keys |= set(_parse_env_file(legacy).keys())
    return {k: v for k, v in env.items() if k not in strip_keys}


# H11b critical finding 2: the `cc:` route's own child env -- everything
# `tool_child_env` already strips PLUS every ANTHROPIC_*/CLAUDE_CODE_*
# variable and the bare CLAUDECODE marker, so the installed `claude`
# binary's own subscription login (never ours to read or replay) is
# ALWAYS what a `cc:` turn uses -- never an ambient ANTHROPIC_API_KEY/
# AUTH_TOKEN/BASE_URL this process happened to have (from the env file, a
# real shell var, or a Databricks passthrough setup for `ant:`), and never
# an OUTER Claude Code session's own identity (CLAUDE_CODE_SESSION_ID,
# CLAUDE_CODE_MESSAGING_SOCKET/_TOKEN, CLAUDE_EFFORT, ...) this halo
# process itself happens to have inherited from running nested inside one.
# Used for BOTH the `claude` subprocess's own env AND (transitively, since
# an MCP stdio child inherits its spawning process's env -- verified live,
# H11 report) the `ccbridge` child's env.
def cc_child_env(env: dict, *, env_file_path: Optional[Path] = None) -> dict:
    stripped = tool_child_env(env, env_file_path=env_file_path)
    # `CLAUDE*` (not just `CLAUDE_CODE_*`) also catches CLAUDECODE and
    # the owner's OWN hook/skill template vars (CLAUDE_EFFORT, CLAUDE_PROJECT_
    # DIR, CLAUDE_PLUGIN_ROOT, CLAUDE_ENV_FILE, ...) -- verified live from
    # INSIDE an actual nested launch: CLAUDE_EFFORT alone survived a
    # narrower `CLAUDE_CODE_`-only prefix check because it has no "_CODE_"
    # in it, yet is explicitly named in finding 2's own repro. None of
    # the owner's own `CLAUDE*` namespace means anything to the real `claude`
    # binary, so the broader prefix costs nothing.
    return {k: v for k, v in stripped.items() if not k.startswith("CLAUDE") and not k.startswith("ANTHROPIC_")}


def load_settings_env_chain(cwd: Path, *, trusted: "bool | None" = None) -> dict:
    """
    Merge env blocks from settings files in precedence order.
    Returns merged dict (later wins).

    N2a (1.0.1 final pass): `trusted` reuses the exact trust check `resolve_
    settings`'s own callers already compute before passing it THAT function's
    own `trusted=` parameter (`config.claude_json.is_trusted(cwd, load_claude_
    json())`) -- `None` (every pre-existing call site, unchanged) now means
    "compute it here" instead of the old, unconditional "always include
    project/local regardless of trust". An untrusted `cwd` skips THIS
    function's own project (`<cwd>/.claude/settings.json`) and local
    (`<cwd>/.claude/settings.local.json`) `env` blocks entirely -- managed/
    user settings are never trust-gated (same as `resolve_settings`'s own
    `_apply_trust_filter`) and are always included. Before this fix, an
    untrusted project could plant a `DATABRICKS_HOST`/`ANTHROPIC_BASE_URL`
    here that `resolve_databricks()`'s own settings-chain re-derivation (and
    every other bare caller below) would happily read, bypassing the exact
    trust gate `resolve_settings` already enforces for a real session's
    `env` block.

    1.0.1 part 2 fixpass finding 3: managed settings now come from
    `config.paths.managed_settings_files()` (every `*.json` under the real
    per-OS managed dir, `/etc/claude-code`/`/Library/Application
    Support/ClaudeCode`/`C:\\Program Files\\ClaudeCode` -- `BRIDGE_TEST_
    MANAGED_DIR`-overridable, same as every other managed-settings reader
    in this tree) instead of a Windows-only, hardcoded `%LOCALAPPDATA%\\
    Claude\\managed-settings.json` that silently found nothing on Linux/
    macOS and ignored `BRIDGE_TEST_MANAGED_DIR`; "user" settings now come
    from `config.paths.claude_config_dir()` (honors `CLAUDE_CONFIG_DIR`)
    instead of a hardcoded `home()/".claude"`. Verified bug: a Linux work
    VM whose gateway env lived in `CLAUDE_CONFIG_DIR`'s settings.json (or a
    real managed-settings file) was invisible to this chain, so every
    `dbx:` ref resolved through it (doctor, `/providers`, `resolve_
    databricks()`'s own bare-env steps 2/3) read as "not configured".
    """
    from halo_harness.config.paths import claude_config_dir, managed_settings_files
    if trusted is None:
        from halo_harness.config.claude_json import is_trusted, load_claude_json
        trusted = is_trusted(cwd, load_claude_json())
    merged = {}
    for managed_path in managed_settings_files():
        try:
            with open(managed_path, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data.get("env"), dict):
                    merged.update(data["env"])
        except (json.JSONDecodeError, OSError):
            pass

    # <claude_config_dir()>/settings.json (CLAUDE_CONFIG_DIR-aware)
    user_settings = claude_config_dir() / "settings.json"
    if user_settings.exists():
        try:
            with open(user_settings, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data.get("env"), dict):
                    merged.update(data["env"])
        except (json.JSONDecodeError, OSError):
            pass

    # <cwd>/.claude/settings.json + <cwd>/.claude/settings.local.json --
    # N2a: skipped entirely for an UNTRUSTED cwd (same gate `resolve_
    # settings`'s own `_apply_trust_filter` applies to a real session's
    # `env` block; deny/ask rules are a `resolve_settings`-only concern and
    # were never read here regardless).
    if trusted:
        project_settings = cwd / ".claude" / "settings.json"
        if project_settings.exists():
            try:
                with open(project_settings, encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data.get("env"), dict):
                        merged.update(data["env"])
            except (json.JSONDecodeError, OSError):
                pass

        local_settings = cwd / ".claude" / "settings.local.json"
        if local_settings.exists():
            try:
                with open(local_settings, encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data.get("env"), dict):
                        merged.update(data["env"])
            except (json.JSONDecodeError, OSError):
                pass

    return merged


def listing_effective_env(cwd: Optional[Path] = None, settings_flag: Optional[str] = None) -> dict:
    """1.0.1 part 2 fixpass finding 3: the merged env a credential-LISTING
    surface with no real `Settings` object of its own (`/providers`, the
    init tabs, `doctor`'s provider summary -- never the parse-time gate,
    which is override-only and never reads credentials at all; a real
    session passes its own `Settings.effective_env` directly instead of
    calling this) uses to decide "is this provider's key/token present" --
    `config.settings.resolve_settings(cwd).effective_env`, the SAME chain
    (shell < user < trusted project/local < policy) a real session
    resolves, so these surfaces never disagree with it just because a
    credential lives only in a settings.json `env` block. A cheap,
    read-only, no-network call (only local JSON files); any failure (a
    malformed settings file, ...) falls back to bare `os.environ` -- the
    unchanged pre-fix behavior for whichever caller hit it, never a crash.

    Findings 22/23 (2.0.1): `cwd` (an explicit `--cwd` flag value, when the
    caller has one) and `settings_flag` (an explicit `--settings` flag
    value) are both threaded straight into `resolve_settings`, exactly like
    a real session's `headless.build_session` already does -- before this
    fix neither ever reached here, so `/providers`/doctor/the init tabs
    could disagree with what a real session launched with either flag
    would resolve. Both default to `None` (today's behavior: process
    `Path.cwd()`, no settings-flag override) for every caller that has
    neither to give."""
    try:
        from halo_harness.config.claude_json import is_trusted, load_claude_json
        from halo_harness.config.settings import resolve_settings
        where = cwd or Path.cwd()
        # Trust-aware like a real session: an untrusted folder's project/local
        # settings never show up as "picked up" credentials in a listing.
        return resolve_settings(
            where, settings_flag=settings_flag, trusted=is_trusted(where, load_claude_json()),
        ).effective_env
    except Exception:
        return dict(os.environ)


def _settings_fallback_value(env: dict, key: str) -> Optional[str]:
    """Finding 20 (2.0.1): `env.get(key)`, falling back to the settings-env
    chain (`load_settings_env_chain(Path.cwd())`) when `env` is the bare
    `os.environ` a caller gets by not passing one -- the exact same gate
    `resolve_databricks`'s own settings-chain re-derivation already uses
    (`env is os.environ`). A caller that already handed in a merged
    `Settings.effective_env` (a real session, or a listing surface via
    `listing_effective_env`) is unaffected: that env dict already IS the
    trust-filtered chain, so re-deriving it again here would be redundant,
    not wrong -- which is why this only fires for the bare-os.environ case,
    matching Databricks exactly. Used by `resolve_openrouter`/`resolve_
    anthropic` (and TypeSafe's own inline check in `providers.enablement.
    credentials_present`) to give all three the fallback Databricks already
    had, so `credentials_present` stops disagreeing with what a real
    session (which always passes `effective_env` explicitly) would
    resolve."""
    value = env.get(key)
    if value:
        return value
    if env is not os.environ:
        return None
    return load_settings_env_chain(Path.cwd()).get(key)


def _settings_fallback_base_url(env: dict, name: str, default: str) -> str:
    """finding 5 (W6a): companion to `_settings_fallback_value` above, for
    a base-URL field instead of a required credential. `env_compat` only
    ever matches a HALO_/BRIDGE_/ROLO_CLAUDE_-prefixed override (the
    harness's own knob-naming convention) against whatever `env` it was
    given -- it had no settings-chain fallback of its own, unlike the
    MATCHING credential lookup (`_settings_fallback_value`) just above. A
    gateway setup's key could therefore resolve from a trusted project/
    user settings.json `env` block while its own base URL silently kept
    the real provider's default host, sending that gateway key to
    api.anthropic.com/openrouter.ai instead. Same gate as `_settings_
    fallback_value`: only a BARE `os.environ` call ever consults the
    settings chain at all (a caller that already handed in a real
    session's merged `effective_env` is unaffected, matching Databricks'
    own settings-chain re-derivation gate exactly) -- and, like that
    function, this reads the settings chain's BARE name directly (never
    bare `os.environ`'s own un-prefixed name, which stays reserved for
    Databricks' identically-named Claude-Code-compatible vars, per
    `resolve_anthropic`'s own docstring)."""
    value = env_compat(name, env)
    if value:
        return value
    if env is os.environ:
        value = load_settings_env_chain(Path.cwd()).get(name)
        if value:
            return value
    return default


@dataclass
class OrConfig:
    """OpenRouter configuration."""
    api_key: str
    base_url: str = "https://openrouter.ai/api/v1"


def resolve_openrouter(env: dict | None = None) -> OrConfig | None:
    """
    Resolve OpenRouter config from `env` (must-do 6: the harness passes
    `Settings.effective_env` -- shell < user < trusted project/local <
    flag < policy -- so a settings.json `env` block can supply the key;
    the proxy's own callers pass nothing and keep reading bare
    `os.environ`, unchanged). Returns None if OPENROUTER_API_KEY not set.

    Finding 20 (2.0.1): a bare call (no `env`) also falls back to the
    settings-env chain (`_settings_fallback_value`), same as `resolve_
    databricks` already did -- a key living only in a trusted project/user
    settings.json `env` block is no longer invisible to auto-detection.

    finding 5 (W6a): `base_url` gets the SAME settings-chain fallback now
    (`_settings_fallback_base_url`) -- it used to come from `env_compat`
    alone (no settings-chain awareness at all), so an OpenRouter-proxy
    key resolved from settings while its own base URL silently kept
    `https://openrouter.ai/api/v1`, sending that proxy key straight to
    the real OpenRouter API instead.
    """
    env = env if env is not None else os.environ
    api_key = _settings_fallback_value(env, "OPENROUTER_API_KEY")
    if not api_key:
        return None
    base_url = _settings_fallback_base_url(env, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    return OrConfig(api_key=api_key, base_url=base_url)


def resolve_openrouter_management_key(env: dict | None = None) -> "str | None":
    """H15 part 2 addendum 4 (corrected): `OPENROUTER_MANAGEMENT_KEY` -- a
    SEPARATE, higher-privilege key OpenRouter's own `/credits` endpoint
    requires (never the ordinary inference `OPENROUTER_API_KEY`). `None`
    when not set -- the account-wide credits figure is then simply skipped
    (`providers/openrouter_account.py`'s own 3-way fallback), never an
    error. Same `env` convention as `resolve_openrouter` (the harness's own
    `Settings.effective_env` chain, or bare `os.environ`)."""
    env = env if env is not None else os.environ
    return env.get("OPENROUTER_MANAGEMENT_KEY") or None


@dataclass
class AntConfig:
    """Direct (non-Databricks, non-OpenRouter) Anthropic API configuration
    (H5 scope C's `ant:` provider): `ANTHROPIC_API_KEY` against
    `api.anthropic.com` directly. Deliberately never reads bare
    `os.environ`'s own `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL` --
    those are Databricks' (or another gateway's) own Claude-Code-
    compatible env vars at the owner's work box, and conflating them here
    would silently point `ant:` at the wrong host for whoever has that
    pair set in their shell. finding 5 (W6a): a trusted project/user
    settings.json `env` block's own (bare-named) `ANTHROPIC_BASE_URL` is
    a different, explicitly-scoped signal -- not ambient shell state --
    so `resolve_anthropic` DOES fall back to it, same as it already did
    for `ANTHROPIC_API_KEY`."""
    api_key: str
    base_url: str = "https://api.anthropic.com"


def resolve_anthropic(env: dict | None = None) -> AntConfig | None:
    """None if `ANTHROPIC_API_KEY` isn't set -- the acceptance contract is
    "`ant:` skipped unless an ANTHROPIC_API_KEY exists", not an error.

    Finding 20 (2.0.1): a bare call (no `env`) falls back to the settings-
    env chain the same way `resolve_openrouter`/`resolve_databricks` do
    (`_settings_fallback_value`) -- never collides with Databricks' own use
    of `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`, since this reads
    `ANTHROPIC_API_KEY` specifically.

    finding 5 (W6a): `base_url` now gets the SAME settings-chain fallback
    (`_settings_fallback_base_url`) -- it used to come from `env_compat`
    alone, which never consults the settings chain at all, so a gateway
    key resolved from a trusted settings.json `env` block while its own
    base URL silently kept `https://api.anthropic.com`, sending that
    gateway key to the real Anthropic API instead."""
    env = env if env is not None else os.environ
    api_key = _settings_fallback_value(env, "ANTHROPIC_API_KEY")
    if not api_key:
        return None
    base_url = _settings_fallback_base_url(env, "ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    return AntConfig(api_key=api_key, base_url=base_url)


@dataclass
class DbxConfig:
    """Databricks configuration. H14 scope A: `host` is ALWAYS the bare
    workspace root (never a gateway path) -- every API call (`/api/2.0/
    serving-endpoints`, `/serving-endpoints/<name>/invocations`,
    `/ai-gateway/...`) builds from it. `anthropic_gateway` is the full
    `<root>/ai-gateway/anthropic` URL, remembered when the ORIGINAL value
    this config was resolved from actually carried that suffix (e.g.
    Claude Code's own `ANTHROPIC_BASE_URL`) -- None when the source was a
    bare root to begin with (DATABRICKS_HOST, ~/.databrickscfg, ...);
    scope D can still always build the gateway URL from `host` regardless.
    H14 scope B: `custom_headers` is `ANTHROPIC_CUSTOM_HEADERS` (one or
    more "Name: value" lines from the settings env) parsed into a plain
    dict, sent on every Databricks request."""
    host: str
    token: str
    anthropic_gateway: Optional[str] = None
    custom_headers: dict = field(default_factory=dict)


_DBX_HOST_SUFFIXES = (".databricks.com", ".azuredatabricks.net", ".gcp.databricks.com")


def looks_like_databricks_host(url_or_host: str) -> bool:
    """True if the hostname (bare host or full URL) ends with a known Databricks domain suffix, never loopback."""
    if not url_or_host:
        return False
    if url_or_host in ("127.0.0.1", "localhost", "::1"):
        return False
    if "://" in url_or_host:
        parsed = urllib.parse.urlparse(url_or_host)
        hostname = parsed.hostname or ""
    else:
        hostname = url_or_host
    hostname = hostname.lower()
    return any(hostname.endswith(suffix) for suffix in _DBX_HOST_SUFFIXES)


def load_databrickscfg(path: Path) -> DbxConfig | None:
    """Parse the [DEFAULT] section of a databricks-cli style config file; None if missing/unparseable/incomplete."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return None
    in_default = False
    host = None
    token = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            in_default = (section == "DEFAULT")
            continue
        if not in_default or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        if key.lower() == "host":
            host = value
        elif key.lower() == "token":
            token = value
    if host and token:
        return DbxConfig(host=host.rstrip("/"), token=token)
    return None


def load_ucode_settings(path: Path) -> DbxConfig | None:
    """H8 scope F must-do (`ug`-compatibility note): Databricks' own
    unity-gateway CLI (`ug`) writes its resolved gateway URL/token to
    `~/.claude/ucode-settings.json` -- read-only here, same "None if
    missing/unparseable/incomplete" contract as `load_databrickscfg`. The
    exact key names aren't documented publicly, so every plausible spelling
    a gateway-config JSON file would plausibly use is tried, first match
    wins per field (never mixes a host from one key with a token from
    another key of the SAME candidate list's LATER, lower-priority entry --
    each list is tried in order, first hit stops that field's own search).

    H9 whole-tree review finding 31: nothing stops this file from being (or
    from being hand-edited into) the SAME shape as a Claude Code `--settings`
    JSON file instead of a purpose-built gateway-config shape -- an `env`
    block holding `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` (this is
    DIFFERENT from `resolve_databricks` steps 2/3 pulling those same two
    names out of a *process* env or the settings-chain env: those never see
    this file at all, since it is read directly off disk, only ever reaching
    an env dict if something else already merged it there), or a top-level
    `apiKeyHelper` command string that must be RUN to obtain the token. So:
    `env` is added as one more nest candidate (flattened exactly like
    `gateway`/`databricks`/`workspace`/`default` above) and
    `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` are added to the host/token
    key-name candidate lists (so a Claude-Code-settings-shaped `env` block
    resolves too, whether nested under `env` or, unusually, top-level); if
    every key-name candidate still leaves the token unset, a top-level or
    nested `apiKeyHelper` string is run (see `_run_api_key_helper`) and its
    output used as the token. Not independently verified against a real
    `ug`-produced file -- none exists on the box this fix was written on;
    verify on a box with `ug` installed before relying on this in
    production."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # A gateway-shaped file may nest its own fields under "gateway"/
    # "databricks"/"workspace"/"env" -- flatten one level so a top-level OR
    # a nested shape both resolve the same way. "env" covers a Claude Code
    # `--settings`-shaped file (H9 finding 31).
    candidates = [data]
    for nest_key in ("gateway", "databricks", "workspace", "default", "env"):
        nested = data.get(nest_key)
        if isinstance(nested, dict):
            candidates.append(nested)
    host = token = None
    for candidate in candidates:
        if host is None:
            for key in ("gateway_url", "host", "url", "base_url", "workspace_url", "workspace_host",
                        "ANTHROPIC_BASE_URL"):
                value = candidate.get(key)
                if isinstance(value, str) and value:
                    host = value
                    break
        if token is None:
            for key in ("token", "api_token", "access_token", "gateway_token", "auth_token",
                        "ANTHROPIC_AUTH_TOKEN"):
                value = candidate.get(key)
                if isinstance(value, str) and value:
                    token = value
                    break
    if token is None:
        for candidate in candidates:
            helper = candidate.get("apiKeyHelper")
            if isinstance(helper, str) and helper.strip():
                token = _run_api_key_helper(helper)
                if token:
                    break
    if host and token:
        return DbxConfig(host=host.rstrip("/"), token=token)
    return None


def _run_api_key_helper(command: str) -> Optional[str]:
    """H9 whole-tree review finding 31: run a Claude Code `--settings`-style
    `apiKeyHelper` command (a shell command string, exactly like Claude Code
    itself invokes it) and return its stripped stdout as a token. Best-
    effort ONLY -- a missing interpreter, non-zero exit, timeout, or empty
    stdout all just return None (same as a missing key would), never raise;
    `load_ucode_settings` is read-only/optional config discovery, not
    something that should ever crash a launch."""
    try:
        result = subprocess.run(command, shell=True, capture_output=True,
                                 text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or "").strip()
    return output or None


def parse_custom_headers(raw: Optional[str]) -> dict[str, str]:
    """H14 scope B: `ANTHROPIC_CUSTOM_HEADERS`-shaped value (Claude Code's
    own settings.json `env` block convention) -- one or more "Name: value"
    lines -- parsed into a plain header-name -> value dict. Blank lines and
    any line without a ':' are skipped; a later line reusing the same name
    (case-insensitively) replaces the earlier one, keeping that later
    occurrence's own name spelling."""
    result: dict[str, str] = {}
    if not raw:
        return result
    lower_seen: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        prior = lower_seen.get(name.lower())
        if prior is not None and prior != name:
            result.pop(prior, None)
        result[name] = value
        lower_seen[name.lower()] = name
    return result


_DBX_DEFAULT_HEADERS = {"x-databricks-use-coding-agent-mode": "true"}


def merge_databricks_headers(custom_headers: Optional[dict], explicit_headers: Optional[dict] = None) -> dict:
    """H14 scope B: the final header dict for a Databricks request --
    `x-databricks-use-coding-agent-mode: true` by default, overridden
    (case-insensitive name match) by `ANTHROPIC_CUSTOM_HEADERS`
    (`custom_headers`, already parsed by `parse_custom_headers`), overridden
    in turn by any explicit `--header`/config-supplied headers. Sent on
    every Databricks request, native passthrough and chat alike."""
    result: dict[str, str] = dict(_DBX_DEFAULT_HEADERS)
    lower_to_key = {k.lower(): k for k in result}
    for source in (custom_headers, explicit_headers):
        for name, value in (source or {}).items():
            existing_key = lower_to_key.get(name.lower())
            if existing_key is not None and existing_key != name:
                del result[existing_key]
            result[name] = value
            lower_to_key[name.lower()] = name
    return result


def resolve_databricks(env: dict | None = None) -> DbxConfig | None:
    """
    Resolve Databricks host+token: BRIDGE_DBX_* override, then filtered
    ANTHROPIC_* (Databricks hosts only, never loopback), then
    DATABRICKS_HOST/TOKEN, then ~/.databrickscfg [DEFAULT]. Never raises.

    must-do 6: when `env` is given (the harness passes `Settings.
    effective_env` -- shell < user < trusted project/local < flag <
    policy, resolved ONCE with the real trust/cwd this session actually
    has), it is used directly for steps 1/2/4 instead of bare
    `os.environ`, and step 3's OWN settings-chain re-derivation (which read
    `%LOCALAPPDATA%` managed settings, gave them the LOWEST precedence,
    let process env beat settings, and ignored `--cwd` entirely -- see
    `load_settings_env_chain`) is skipped, since `env` already IS that
    merged chain, correctly ordered and trust-filtered. The proxy's own
    callers pass nothing and get the exact pre-H2 behavior.

    H14 scope A/B: every branch below returns through `_finalize`, which
    (a) splits the resolved host into its bare workspace root (`.host`)
    plus, when the ORIGINAL value carried it, the full `/ai-gateway/
    anthropic` gateway URL (`.anthropic_gateway`), and (b) parses
    `ANTHROPIC_CUSTOM_HEADERS` (from `env`, falling back to the settings
    chain re-derivation the same way step 3 does) onto `.custom_headers`.
    """
    env = env if env is not None else os.environ
    _settings_env_box: list = []

    def _settings_env() -> dict:
        if not _settings_env_box:
            _settings_env_box.append(load_settings_env_chain(Path.cwd()) if env is os.environ else {})
        return _settings_env_box[0]

    def _custom_headers() -> dict[str, str]:
        raw = env.get("ANTHROPIC_CUSTOM_HEADERS") or _settings_env().get("ANTHROPIC_CUSTOM_HEADERS") or ""
        return parse_custom_headers(raw)

    def _finalize(host_raw: str, token: str) -> DbxConfig:
        stripped = host_raw.rstrip("/")
        root = derive_workspace_root(stripped)
        gateway = f"{root}/ai-gateway/anthropic" if root != stripped else None
        return DbxConfig(host=root, token=token, anthropic_gateway=gateway, custom_headers=_custom_headers())

    # 1. Explicit override -- always wins, unfiltered.
    bridge_host = env_compat("DBX_BASE_URL", env)
    bridge_token = env_compat("DBX_TOKEN", env)
    if bridge_host and bridge_token:
        return _finalize(bridge_host, bridge_token)

    # 2. ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN (Databricks hosts only).
    anth_host = env.get("ANTHROPIC_BASE_URL")
    anth_token = env.get("ANTHROPIC_AUTH_TOKEN")
    if anth_host and anth_token and looks_like_databricks_host(anth_host):
        return _finalize(anth_host, anth_token)

    # 3. The proxy's OWN settings-chain re-derivation -- only when the
    # caller did NOT already hand us a merged env (see docstring above).
    settings_env = _settings_env()
    if settings_env:
        anth_host = settings_env.get("ANTHROPIC_BASE_URL")
        anth_token = settings_env.get("ANTHROPIC_AUTH_TOKEN")
        if anth_host and anth_token and looks_like_databricks_host(anth_host):
            return _finalize(anth_host, anth_token)

    # 4. DATABRICKS_HOST + DATABRICKS_TOKEN -- per field, shell env / env
    # file first, then the settings chain. The settings chain is already
    # TRUST-FILTERED (`load_settings_env_chain` skips project/local layers
    # of an untrusted folder, N2a), which is what keeps an untrusted
    # project's host away from the user's token; so a token exported in
    # the shell may legitimately pair with a host kept in the user's own
    # settings.json env block (a common work-box setup), and vice versa.
    dbx_host = env.get("DATABRICKS_HOST") or settings_env.get("DATABRICKS_HOST")
    dbx_token = env.get("DATABRICKS_TOKEN") or settings_env.get("DATABRICKS_TOKEN")
    if dbx_host and dbx_token:
        return _finalize(dbx_host, dbx_token)

    # 5. H8 scope F must-do: ~/.claude/ucode-settings.json, written by
    # Databricks' own `ug` (unity-gateway) CLI -- a purpose-built, harness-
    # adjacent config file, so it's tried BEFORE the generic (and possibly
    # stale/unrelated-workspace) ~/.databrickscfg below.
    from halo_harness.config.paths import claude_config_dir
    ucode = load_ucode_settings(claude_config_dir() / "ucode-settings.json")
    if ucode is not None:
        return _finalize(ucode.host, ucode.token)

    # 6. ~/.databrickscfg [DEFAULT].
    cfg_file = load_databrickscfg(home() / ".databrickscfg")
    if cfg_file is not None:
        return _finalize(cfg_file.host, cfg_file.token)
    return None


def resolve_databricks_source(env: dict | None = None) -> Optional[str]:
    """H14 scope E: WHICH step of `resolve_databricks`'s own precedence
    chain would supply the config -- a short human label `doctor --work`
    prints as its "token source" line. Mirrors `resolve_databricks` exactly
    (same order, same settings-chain re-derivation gate) as a read-only,
    side-effect-free lookup; None when nothing resolves there either."""
    env = env if env is not None else os.environ
    if env_compat("DBX_BASE_URL", env) and env_compat("DBX_TOKEN", env):
        return "HALO_DBX_BASE_URL/HALO_DBX_TOKEN (explicit override)"
    anth_host, anth_token = env.get("ANTHROPIC_BASE_URL"), env.get("ANTHROPIC_AUTH_TOKEN")
    if anth_host and anth_token and looks_like_databricks_host(anth_host):
        return "ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN (environment)"
    settings_env = load_settings_env_chain(Path.cwd()) if env is os.environ else {}
    if settings_env:
        anth_host = settings_env.get("ANTHROPIC_BASE_URL")
        anth_token = settings_env.get("ANTHROPIC_AUTH_TOKEN")
        if anth_host and anth_token and looks_like_databricks_host(anth_host):
            return "ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN (Claude Code settings.json env)"
    # Mirrors resolve_databricks()'s own step 4: per field, shell env / env
    # file first, then the trust-filtered settings chain.
    dbx_host = env.get("DATABRICKS_HOST") or settings_env.get("DATABRICKS_HOST")
    dbx_token = env.get("DATABRICKS_TOKEN") or settings_env.get("DATABRICKS_TOKEN")
    if dbx_host and dbx_token:
        return "DATABRICKS_HOST/DATABRICKS_TOKEN"
    from halo_harness.config.paths import claude_config_dir
    if load_ucode_settings(claude_config_dir() / "ucode-settings.json") is not None:
        return "~/.claude/ucode-settings.json"
    if load_databrickscfg(home() / ".databrickscfg") is not None:
        return "~/.databrickscfg"
    return None


def resolve_databricks_host_only(env: dict | None = None) -> Optional[str]:
    """H14 scope E: the bare workspace root when SOME step of `resolve_
    databricks`'s own chain has a host but not (that step's own) token --
    "host configured, token missing" doctor's own line needs this to ever
    actually fire in a real run, since `resolve_databricks()` only ever
    returns a host+token PAIR together (any lone host is otherwise
    invisible to a caller). None when a full config already resolves (not
    "host-only" -- just configured) or no step has a host at all."""
    env = env if env is not None else os.environ
    if resolve_databricks(env) is not None:
        return None
    bridge_host = env_compat("DBX_BASE_URL", env)
    if bridge_host:
        return derive_workspace_root(bridge_host)
    anth_host = env.get("ANTHROPIC_BASE_URL")
    if anth_host and looks_like_databricks_host(anth_host):
        return derive_workspace_root(anth_host)
    settings_env = load_settings_env_chain(Path.cwd()) if env is os.environ else {}
    anth_host = settings_env.get("ANTHROPIC_BASE_URL")
    if anth_host and looks_like_databricks_host(anth_host):
        return derive_workspace_root(anth_host)
    dbx_host = env.get("DATABRICKS_HOST") or settings_env.get("DATABRICKS_HOST")
    if dbx_host:
        return derive_workspace_root(dbx_host)
    return None


# ---------------------------------------------------------------------------
# H14 scope C: model defaults/aliases "at work" -- Claude Code's own settings
# env (ANTHROPIC_MODEL/ANTHROPIC_DEFAULT_*_MODEL) sets the harness's default
# model and what bare opus/sonnet/haiku resolve to, instead of the cc:/ant:
# subscription route or the hardcoded OpenRouter default.
# ---------------------------------------------------------------------------

_WORK_ENV_SIGNAL_KEYS = (
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)


def _work_env_signal(env: dict) -> dict:
    """The `_WORK_ENV_SIGNAL_KEYS` values from `env`, falling back to the
    settings-chain re-derivation (same `env is os.environ` gate
    `resolve_databricks` itself uses -- must-do 6) so a caller with no
    merged `Settings.effective_env` to pass (most bare `parse_model_ref`
    call sites) still sees a work env that lives ONLY in Claude Code's
    settings.json `env` block, never just the raw process environment."""
    settings_env = load_settings_env_chain(Path.cwd()) if env is os.environ else {}
    return {k: env.get(k) or settings_env.get(k) for k in _WORK_ENV_SIGNAL_KEYS}


def databricks_work_signal_present(env: dict | None = None) -> bool:
    """True when `env` (or the settings-chain re-derivation, same gate as
    `_work_env_signal`) carries Claude Code's own Databricks work-box
    signal (`ANTHROPIC_MODEL`/`ANTHROPIC_DEFAULT_*_MODEL`) -- WITHOUT also
    requiring a full host+token to already resolve (unlike
    `databricks_work_env_active`). Used wherever a caller needs to tell
    "this host came from Claude Code's own work settings" apart from an
    UNRELATED ambient `DATABRICKS_HOST`/`ANTHROPIC_BASE_URL` a box might
    have set for some other Databricks tool -- e.g. `init --preset work`'s
    own "host already known, ask only for the token" shortcut (scope I)
    must never fire off a bare, incidental `DATABRICKS_HOST` alone."""
    env = env if env is not None else os.environ
    return any(_work_env_signal(env).values())


def databricks_work_env_active(env: dict | None = None) -> bool:
    """True when `env` carries Claude Code's own Databricks work-box env
    shape (`ANTHROPIC_MODEL` and/or `ANTHROPIC_DEFAULT_*_MODEL`) AND a
    Databricks config actually resolves from it -- the signal that tells
    "Claude Code's settings env, pointed at Databricks" (base URL or
    DATABRICKS_HOST) apart from an unrelated ~/.databrickscfg/ucode-
    settings.json a box may also have for other purposes (those never set
    these Claude-Code-specific names, so this is always False for them)."""
    env = env if env is not None else os.environ
    if not databricks_work_signal_present(env):
        return False
    return resolve_databricks(env) is not None


_BARE_TIER_ENV_VAR = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}
# Pinned Databricks Claude endpoint fallback when the matching
# ANTHROPIC_DEFAULT_*_MODEL var is unset (brief scope C).
_BARE_TIER_DBX_FALLBACK = {
    "opus": "databricks-claude-opus-5-5",
    "sonnet": "databricks-claude-sonnet-5-5",
    "haiku": "databricks-claude-haiku-4-5",
}


def databricks_default_model_for_tier(tier: str, env: dict | None = None) -> Optional[str]:
    """bare `opus`/`sonnet`/`haiku` -> the bare (unprefixed) Databricks
    endpoint name Claude Code's own `ANTHROPIC_DEFAULT_*_MODEL` picks at
    work, or the pinned Databricks Claude endpoint fallback when that var
    is unset; None for any other tier name. Falls back to the settings-
    chain re-derivation the same way `_work_env_signal` does, so a value
    living only in Claude Code's settings.json is found too."""
    env = env if env is not None else os.environ
    var = _BARE_TIER_ENV_VAR.get(tier)
    if var is None:
        return None
    return _work_env_signal(env).get(var) or _BARE_TIER_DBX_FALLBACK[tier]


def derive_workspace_root(base_url: str) -> str:
    """Strip a trailing /ai-gateway/anthropic[/v1[/messages]] suffix (and any trailing slash) from a Databricks base URL, returning the bare workspace root."""
    stripped = base_url.rstrip("/")
    root = re.sub(r"/ai-gateway/anthropic(?:/v1(?:/messages)?)?/?$", "", stripped)
    return root.rstrip("/")


def extract_custom_headers(incoming_headers) -> dict[str, str]:
    """Copy incoming request headers for upstream Databricks relay, dropping hop-by-hop/bridge-internal ones."""
    result = {}
    skip_lower = {"x-api-key", "authorization", "host", "content-length", "content-type",
                  "accept-encoding", "connection", "transfer-encoding"}
    for name, value in incoming_headers.items():
        lower_name = name.lower()
        if lower_name in skip_lower or lower_name.startswith("x-bridge-"):
            continue
        result[name] = value
    return result


def load_routes(path: Path | None) -> dict:
    """Load routes JSON file, return empty dict if missing/invalid."""
    if path is None or not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def redact(s: str | None) -> str:
    """Keep first 4 chars + '...' for non-empty strings, else '<unset>'."""
    if not s:
        return "<unset>"
    if len(s) <= 4:
        return s
    return s[:4] + "..."


@dataclass
class BridgeConfig:
    """Complete bridge configuration for --config output."""
    state_dir: Path
    openrouter: Optional[dict] = None
    databricks: Optional[dict] = None
    routes: dict = field(default_factory=dict)
    env_file_loaded: bool = False


def resolve_config() -> BridgeConfig:
    """
    Resolve all configuration without network calls.
    Secrets are redacted via redact().
    """
    # Load env file if present (2.0.0 fixpass finding 4: new file, then the
    # legacy one too -- see load_provider_env_files's own docstring).
    env_loaded = bool(load_provider_env_files())

    state_dir = default_state_dir()
    routes_path = state_dir / "routes.json" if state_dir.exists() else None
    routes = load_routes(routes_path)

    # OpenRouter config (redacted)
    or_config = resolve_openrouter()
    or_dict = None
    if or_config:
        or_dict = {
            "api_key": redact(or_config.api_key),
            "base_url": or_config.base_url,
        }

    # Databricks config (redacted)
    dbx_config = resolve_databricks()
    dbx_dict = None
    if dbx_config:
        dbx_root = dbx_config.host  # already the bare root (H14 scope A)
        dbx_dict = {
            "host": dbx_config.host,
            "token": redact(dbx_config.token),
            "workspace_root": dbx_root,
            "anthropic_gateway": dbx_config.anthropic_gateway,
            "custom_header_names": sorted(dbx_config.custom_headers),
            "routes": {
                "invocations": f"{dbx_root}/serving-endpoints/<name>/invocations",
                "mlflow_chat": f"{dbx_root}/ai-gateway/mlflow/v1/chat/completions",
                "claude_passthrough": f"{dbx_root}/ai-gateway/anthropic/v1/messages",
            },
        }

    return BridgeConfig(
        state_dir=state_dir,
        openrouter=or_dict,
        databricks=dbx_dict,
        routes=routes,
        env_file_loaded=env_loaded,
    )


def ensure_token(state_dir: Path) -> str:
    """
    Read token from state_dir/token if exists, else generate and write.
    """
    token_file = state_dir / "token"
    if token_file.exists():
        try:
            return token_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    token = secrets.token_urlsafe(32)
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        token_file.write_text(token, encoding="utf-8")
        if os.name != "nt":
            token_file.chmod(0o600)
    except OSError:
        pass
    return token


def bridge_py_sha1() -> str:
    """Tree hash: sha1 over the running proxy's bridge.py bytes, followed by
    every halo_harness/providers/*.py file's bytes in sorted-filename order
    (each entry prefixed by its own name so a rename changes the digest too).

    bridge.py's path is resolved from sys.modules["__main__"] whenever the
    running process WAS launched as a bridge.py-shaped script (detected via
    the cmd_serve attribute, which only a real -- or byte-mutated -- copy of
    bridge.py defines, regardless of what the file is actually named) so a
    byte-mutated copy run directly for the launcher's stale-server-restart
    test hashes as ITSELF, not the installed original. Every other caller
    (``python -m halo_harness ...``, a direct ``import bridge``) falls back
    to the real bridge.py installed as this package's sibling, which the
    packaging layout guarantees (py-modules=["bridge"] alongside the
    halo_harness package, in the repo root or in site-packages alike).
    """
    providers_dir = Path(__file__).resolve().parent
    main_mod = sys.modules.get("__main__")
    main_file = getattr(main_mod, "__file__", None)
    if main_mod is not None and hasattr(main_mod, "cmd_serve") and main_file:
        bridge_path = Path(main_file)
    else:
        bridge_path = providers_dir.parents[1] / "bridge.py"
    h = hashlib.sha1()
    try:
        h.update(b"bridge.py\x00")
        h.update(bridge_path.read_bytes())
        for p in sorted(providers_dir.glob("*.py")):
            h.update(p.name.encode("utf-8") + b"\x00")
            h.update(p.read_bytes())
    except OSError:
        return "0" * 40
    return h.hexdigest()


def write_server_json(state_dir: Path, pid: int, port: int) -> None:
    """Write server.json with service info."""
    data = {
        "service": "claude-bridge",
        "version": __version__,
        "hash": bridge_py_sha1(),
        "pid": pid,
        "port": port,
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    server_file = state_dir / "server.json"
    try:
        with open(server_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts sensitive headers in log messages."""

    SENSITIVE_PATTERNS = [
        (r'(?i)(x-api-key|authorization|bearer)(\s*:?\s*)([^\s,;"]+)', r'\1\2[REDACTED]'),
        (r'(?i)"(api_key|token|key|secret)"\s*:\s*"[^"]*"', r'"\1":"[REDACTED]"'),
        (r'(?i)(password|passwd|pwd)\s*=\s*[^\s&]+', r'\1=[REDACTED]'),
    ]

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        for pattern, replacement in self.SENSITIVE_PATTERNS:
            msg = re.sub(pattern, replacement, msg)
        return msg


def setup_logging(state_dir: Path) -> logging.Logger:
    """
    Configure logging with RotatingFileHandler to state_dir/bridge.log.
    Returns the 'bridge' logger.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    log_file = state_dir / "bridge.log"

    # Remove any existing handlers from the bridge logger
    logger = logging.getLogger("bridge")
    for hdlr in logger.handlers[:]:
        logger.removeHandler(hdlr)

    handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    formatter = RedactingFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    return logger


def jdumps(obj: Any) -> bytes:
    """JSON dumps with ensure_ascii=False, encode to UTF-8 with replacement."""
    return json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace")


_IMAGE_FLAT_TOKENS = 1600


def _strip_image_payloads_for_estimate(obj):
    """Return (structurally-similar copy of obj, image_count): every embedded
    base64 image payload -- OpenAI-ish {"type":"image_url","image_url":
    {"url":"data:...;base64,..."}} or Anthropic-ish {"type":"image","source":
    {"data":...}} -- is replaced by a short placeholder and counted, so
    estimate_tokens can charge it a flat per-image cost instead of its raw
    (post-base64-inflation) byte length (finding 1)."""
    count = 0

    def walk(o):
        nonlocal count
        if isinstance(o, dict):
            if o.get("type") == "image_url" and isinstance(o.get("image_url"), dict) \
                    and isinstance(o["image_url"].get("url"), str) and "base64," in o["image_url"]["url"]:
                count += 1
                return {"type": "image_url", "image_url": {"url": "(image)"}}
            if o.get("type") == "image" and isinstance(o.get("source"), dict) and "data" in o["source"]:
                count += 1
                new_source = dict(o["source"])
                new_source["data"] = "(image)"
                return {**o, "source": new_source}
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(item) for item in o]
        return o

    return walk(obj), count


def estimate_tokens(obj: Any) -> int:
    """Estimate tokens as len(json)/4 for everything except embedded base64
    image payloads, which are counted at a flat ~1600 tokens each instead of
    their raw byte length -- otherwise one large screenshot in history
    inflates the estimate so much the up-front clamp floors max_tokens at 1,
    giving 1-token replies every later turn (finding 1). Minimum 1."""
    stripped, image_count = _strip_image_payloads_for_estimate(obj)
    json_str = json.dumps(stripped, ensure_ascii=False)
    return max(1, len(json_str) // 4 + image_count * _IMAGE_FLAT_TOKENS)


def dump_debug(state_dir: Path, kind: str, payload: Any) -> None:
    """Write debug dump if HALO_DUMP=1 (legacy BRIDGE_DUMP=1 still honoured)."""
    if env_compat("DUMP") != "1":
        return
    dump_dir = state_dir / "dumps"
    dump_dir.mkdir(parents=True, exist_ok=True)
    dump_file = dump_dir / f"{time.time():.3f}-{kind}.json"
    try:
        with open(dump_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except OSError:
        pass

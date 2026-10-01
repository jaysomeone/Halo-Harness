"""halo_harness.providers.cc_models -- H11 Part A: alias tables for the
`cc:` route (the installed `claude` binary, driven headlessly under the
user's OWN Claude subscription login -- halo never reads or
replays Claude Code's OAuth credentials, see docs/harness/H11-brief.md)
and the `ant:` route's matching first-class aliases (the SAME six model
names resolved to real API ids, for ANTHROPIC_API_KEY pay-as-you-go
access). Also the one place that shells out to `claude auth status` --
NEVER the credentials file itself -- to answer "is the subscription
available" for doctor.py, the bare-alias resolver in model.py, and the
`cc:` transport's own preflight check.

Static tables below are the SEED/FALLBACK, verified live against the
installed `claude` 2.1.281/2.1.284 on this box (2026-09-28 -- see the
brief's own "Verified facts" and this milestone's report): `fable` ->
`claude-fable-5-1`, `opus` -> `claude-opus-5-5`, `opus-5`/`opus-5.0` ->
`claude-opus-5`, `opus-4.8` -> `claude-opus-4-8`, `opus-4.6` ->
`claude-opus-4-6`, `sonnet` -> Claude Code's OWN `sonnet` alias (today
`claude-sonnet-5-5`), `sonnet-5` -> `claude-sonnet-5`, `haiku` -> Claude
Code's own `haiku` alias (today the dated snapshot
`claude-haiku-4-5-20251001`). `refresh_cc_catalog` (`halo models
--cc --refresh`) re-derives the ant: targets for `sonnet`/`haiku` (the two
whose target drifts over time) from a live `-p --max-turns 1` ping's own
`modelUsage` key and caches the result to `<state_dir>/cc-models.json`,
consulted by `resolve_ant_alias`/`profile_fields_for_cc_model` BEFORE this
module's own static fallback -- so a stale hardcoded id is never the only
source once a refresh has run.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# H11 Part A: the same nine bare names route through BOTH `cc:` and `ant:`
# (a full id / an already-prefixed ref / anything not in this table is
# untouched pass-through, exactly like every other alias in this harness).
CC_ALIASES: "dict[str, str]" = {
    "fable": "claude-fable-5-1",
    "opus": "claude-opus-5-5",
    # H15 addendum 2: an EXPLICIT dotted-version name for the current
    # `opus` target, so the `cc:` enumeration (doctor/models/`/model`) can
    # show "cc:opus-5.5" as its own distinguishable row instead of making
    # the reader infer from "cc:opus -> claude-opus-5-5" that 5.5 IS the
    # latest -- same final id as bare "opus", never a second live target.
    "opus-5.5": "claude-opus-5-5",
    "opus-5": "claude-opus-5",
    "opus-5.0": "claude-opus-5",
    "opus-4.8": "claude-opus-4-8",
    "opus-4.6": "claude-opus-4-6",
    # Claude Code's OWN aliases -- passed through unchanged so ITS
    # resolution (today claude-sonnet-5-5 / a dated haiku snapshot) is
    # always the live truth, never a value this table could go stale on.
    "sonnet": "sonnet",
    # Same reasoning as opus-5.5 above, but note the bare "sonnet" alias
    # resolves through Claude Code's OWN live "sonnet" pointer (passthrough,
    # not this table) -- "sonnet-5.5" is pinned to TODAY's concrete id
    # instead, so it keeps meaning 5.5 even after Code's own "sonnet"
    # pointer later moves to a newer point release.
    "sonnet-5.5": "claude-sonnet-5-5",
    "sonnet-5": "claude-sonnet-5",
    "haiku": "haiku",
}

# ant: (ANTHROPIC_API_KEY, the real API) needs a CONCRETE id even for the
# two Claude-Code-alias names above -- seeded from the same live discovery
# (2026-09-28); `resolve_ant_alias` prefers a fresher `cc-models.json`
# cache (from `--refresh`) over these two specifically, since they are the
# only ones whose target can move as Anthropic ships new point releases.
ANT_ALIASES: "dict[str, str]" = {
    "fable": "claude-fable-5-1",
    "opus": "claude-opus-5-5",
    "opus-5.5": "claude-opus-5-5",
    "opus-5": "claude-opus-5",
    "opus-5.0": "claude-opus-5",
    "opus-4.8": "claude-opus-4-8",
    "opus-4.6": "claude-opus-4-6",
    "sonnet": "claude-sonnet-5-5",
    "sonnet-5.5": "claude-sonnet-5-5",
    "sonnet-5": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
}

BARE_ALIAS_NAMES = frozenset(CC_ALIASES)
_DRIFTING_ANT_NAMES = frozenset({"sonnet", "haiku"})  # target moves as Anthropic ships releases

_1M_SUFFIX = "[1m]"

# H8-style vendored fallback tier (model.py's own D10 "Aider-shaped data
# file" pattern), keyed by the CANONICAL id `resolve_cc_alias`/
# `resolve_ant_alias` produce -- "sonnet"/"haiku" (Claude Code's own
# unresolved aliases, the literal `cc:` wire value) are ALSO keys here so
# a `cc:sonnet`/`cc:haiku` session still gets a real profile instead of
# the bare-dataclass guess. context_tokens/max_output_tokens/pricing from
# the OpenRouter `anthropic/*` catalog rows already cached in this box's
# own models.json (verified 2026-09-28); `adaptive_thinking` (brief Part
# A: "adaptive-thinking rule for 4.6+/5.x") is INFORMATIONAL metadata only
# -- surfaced by `halo models --cc` and doctor, never consulted by
# request-building, since a `cc:` turn never builds a raw API request at
# all (Claude Code does) and `ant:` reuses the existing native-Anthropic-
# passthrough path unchanged. True for every opus>=4.6, every 5.x, and
# fable (all adaptive-thinking-capable per Anthropic's own model notes);
# False for haiku.
# H11b finding 7: every current Claude model (subscription or API) accepts
# image input -- `vision=True` on every row here is what makes
# `model.resolve_model_profile`'s cc:/ant: branch actually set
# `ModelProfile.vision=True` (before this fix the fields dict had no
# "vision" key at all, so `fields.get("vision", False)` -- and, until that
# call site was also fixed, the ModelProfile constructor call itself, which
# never even READ this key -- silently defaulted every cc:/ant: model to
# vision=False, the exact bug verified live: "Read of a PNG on cc:fable
# returns ... this model has no vision support configured").
CC_MODEL_TABLE: "dict[str, dict]" = {
    "claude-fable-5-1": {
        "context_tokens": 1_000_000, "max_output_tokens": 128_000, "adaptive_thinking": True, "vision": True,
        "price_in": 0.00001, "price_out": 0.00005,
        "price_cache_read": 0.00000025, "price_cache_write": 0.0000125,
    },
    "claude-opus-5-5": {
        "context_tokens": 1_000_000, "max_output_tokens": 128_000, "adaptive_thinking": True, "vision": True,
        "price_in": 0.000004, "price_out": 0.00002,
        "price_cache_read": 0.0000002, "price_cache_write": 0.000005,
    },
    "claude-opus-5": {
        "context_tokens": 1_000_000, "max_output_tokens": 128_000, "adaptive_thinking": True, "vision": True,
        "price_in": 0.000005, "price_out": 0.000025,
        "price_cache_read": 0.0000005, "price_cache_write": 0.00000625,
    },
    "claude-opus-4-8": {
        "context_tokens": 1_000_000, "max_output_tokens": 128_000, "adaptive_thinking": True, "vision": True,
        "price_in": 0.000005, "price_out": 0.000025,
        "price_cache_read": 0.0000005, "price_cache_write": 0.00000625,
    },
    "claude-opus-4-6": {
        "context_tokens": 1_000_000, "max_output_tokens": 128_000, "adaptive_thinking": True, "vision": True,
        "price_in": 0.000005, "price_out": 0.000025,
        "price_cache_read": 0.0000005, "price_cache_write": 0.00000625,
    },
    "claude-sonnet-5": {
        "context_tokens": 1_000_000, "max_output_tokens": 128_000, "adaptive_thinking": True, "vision": True,
        "price_in": 0.000002, "price_out": 0.00001,
        "price_cache_read": 0.0000002, "price_cache_write": 0.0000025,
    },
    # No direct OpenRouter anthropic/claude-sonnet-5.5 row was cached as of
    # 2026-09-28 -- proxied from claude-sonnet-5's own pricing (same tier,
    # one point release apart) until a refresh observes the real row.
    "claude-sonnet-5-5": {
        "context_tokens": 1_000_000, "max_output_tokens": 128_000, "adaptive_thinking": True, "vision": True,
        "price_in": 0.000002, "price_out": 0.00001,
        "price_cache_read": 0.0000002, "price_cache_write": 0.0000025,
    },
    "claude-haiku-4-5-20251001": {
        "context_tokens": 200_000, "max_output_tokens": 64_000, "adaptive_thinking": False, "vision": True,
        "price_in": 0.000001, "price_out": 0.000005,
        "price_cache_read": 0.0000001, "price_cache_write": 0.00000125,
    },
}
# Claude Code's own unresolved aliases are valid `cc:` wire values too
# (see CC_ALIASES) -- alias them to the same rows in THIS table.
CC_MODEL_TABLE["sonnet"] = CC_MODEL_TABLE["claude-sonnet-5-5"]
CC_MODEL_TABLE["haiku"] = CC_MODEL_TABLE["claude-haiku-4-5-20251001"]


class ClaudeCodeNotFoundError(Exception):
    """No `claude` binary could be resolved (HALO_CLAUDE_EXE/legacy
    BRIDGE_CLAUDE_EXE unset, and none on PATH or at ~/.local/bin) --
    doctor.py's "install Claude Code" case."""


def _split_1m_suffix(bare: str) -> "tuple[str, str]":
    """`claude-sonnet-5[1m]` -> `("claude-sonnet-5", "[1m]")`; Part A:
    "any full id and the `[1m]` suffix pass through unchanged" -- stripped
    only long enough to look an ALIAS up, then reattached verbatim."""
    if bare.endswith(_1M_SUFFIX):
        return bare[:-len(_1M_SUFFIX)], _1M_SUFFIX
    return bare, ""


def resolve_cc_alias(bare: str) -> str:
    """`fable`/`opus`/... -> the value `claude --model` accepts; anything
    NOT one of the nine names (a full id, an already-resolved id, a
    typo) passes through unchanged -- `cc:` is a superset, never a
    closed enum."""
    base, suffix = _split_1m_suffix(bare)
    return CC_ALIASES.get(base, base) + suffix


def resolve_ant_alias(bare: str) -> str:
    """Same nine names -> a real API model id for ANTHROPIC_API_KEY;
    `sonnet`/`haiku` prefer a fresher `--refresh`-cached target (see
    `_cached_ant_alias`) over the static table, since those two drift.
    Anything not in the table (e.g. `ant:claude-opus-4`, already covered
    by test_model.py) passes through unchanged, unaffected by this
    milestone."""
    base, suffix = _split_1m_suffix(bare)
    if base in _DRIFTING_ANT_NAMES:
        cached = _cached_ant_alias(base)
        if cached:
            return cached + suffix
    return ANT_ALIASES.get(base, base) + suffix


# H15 addendum 2: the two names kept as deliberately-moving "latest"
# pointers (see CC_ALIASES' own docstring) -- every OTHER alias is already
# a specific version, so it needs no extra disambiguating note.
_LATEST_POINTER_NOTE = {"opus": "latest Opus", "sonnet": "latest Sonnet"}


def alias_display_detail(alias: str) -> str:
    """`-> <resolved-id>` (plus a "(latest Opus)"/"(latest Sonnet)" note for
    those two names) shown next to every `cc:`/`ant:` alias row in
    `/model`, `halo models` and the init picker (`model_display.
    format_model_row`'s own generic `detail` bracket -- the SAME mechanism
    a Databricks row's `<family> · <path>` tag already uses, so the
    subscription enumeration reads the same way). Without this a reader has
    to already know that `cc:opus`/`cc:opus-5.5` are the same model.
    Resolves through `resolve_ant_alias` (the freshest known id -- a
    `--refresh`-cached target for `sonnet`/`haiku` when one exists, else
    the static table) even for a `cc:` row, since Claude Code's OWN
    unresolved `sonnet`/`haiku` pointers (CC_ALIASES' literal passthrough
    values) are not themselves a useful thing to show."""
    resolved = resolve_ant_alias(alias)
    note = _LATEST_POINTER_NOTE.get(alias)
    return f"-> {resolved}" + (f" ({note})" if note else "")


# ---- claude binary / auth status -------------------------------------------

def resolve_claude_launch_argv() -> "list[str]":
    """argv PREFIX for launching the claude binary (or, in a test, a fake
    stand-in). `BRIDGE_CLAUDE_EXE`, when set, is shlex-split -- a
    DELIBERATE difference from `mcp_setup.find_claude_exe()`'s
    single-path contract (used for --chrome, always a real claude binary):
    Part C's fake claude is a Python script, which Windows' CreateProcess
    cannot exec directly, so a test points this at `"<python> <script>"`
    and gets a real, working argv prefix on every platform. Falls back to
    `mcp_setup.find_claude_exe()` (OS-neutral: PATH, then
    ~/.local/bin/claude[.exe]) wrapped as a one-element list; raises
    ClaudeCodeNotFoundError (never returns None/[]) when nothing resolves,
    so every caller gets one exception type to catch."""
    from halo_harness.config.paths import env_compat
    env_val = env_compat("CLAUDE_EXE")
    if env_val:
        # posix=True (even on Windows): it is the only mode that strips
        # quotes at all, needed for a two-token "<python> <script>" value
        # with spaces in either path -- verified this is safe for a
        # Windows path's own backslashes too, AS LONG AS every path-
        # bearing token is double-quoted (posix mode only treats a
        # backslash as an escape character INSIDE quotes, and only before
        # $/`/"/\/newline there; a BARE unquoted backslash path is
        # mangled -- shlex.split(r'C:\python.exe', posix=True) ==
        # ['C:python.exe']). Every test in this tree that sets this var
        # double-quotes both tokens for exactly this reason.
        return shlex.split(env_val, posix=True)
    from halo_harness.mcp_setup import find_claude_exe
    exe = find_claude_exe()
    if not exe:
        raise ClaudeCodeNotFoundError(
            "claude executable not found (HALO_CLAUDE_EXE unset; looked on PATH and ~/.local/bin)"
        )
    return [exe]


@dataclass(frozen=True)
class ClaudeAuthStatus:
    logged_in: bool
    auth_method: Optional[str] = None
    email: Optional[str] = None
    subscription_type: Optional[str] = None
    version: Optional[str] = None
    raw: dict = field(default_factory=dict)
    # H11b finding 23: a real `claude auth status` timeout must not read as
    # "not installed" (doctor's misleading "install Claude Code" message,
    # verified live) -- set True ONLY on a `subprocess.TimeoutExpired`,
    # never merged with the "binary not found at all" (`None` return) or
    # "ran fine, reported not logged in" (`logged_in=False, timed_out=
    # False`) cases.
    timed_out: bool = False


# H11b critical finding 2: `authMethod` values `claude_auth_status` treats
# as "this really is the user's own claude.ai subscription" -- everything
# else (today just `"api_key"`, seen live when ANTHROPIC_API_KEY is set in
# the checked environment) means "logged in, but not to a subscription
# cc: may spend against" (doctor WARNs, `_preflight_cc` refuses and points
# at `ant:`).
SUBSCRIPTION_AUTH_METHODS = frozenset({"claude.ai"})


def claude_auth_status(*, timeout: float = 10.0, env: Optional[dict] = None) -> Optional[ClaudeAuthStatus]:
    """Runs `claude auth status` and parses its JSON -- NEVER opens
    `~/.claude/.credentials.json` (binding constraint, brief). Returns
    None ONLY when the binary itself can't be found/run at all (doctor's
    "install Claude Code" case); a resolvable binary that reports (or
    prints something unparseable, treated the same as "not logged in" --
    a real `claude auth status` always prints JSON, per this milestone's
    live verification) `loggedIn: false` -- or omits the key -- still
    returns a `ClaudeAuthStatus(logged_in=False, ...)` (doctor's "run
    `claude` once and log in" case), so callers can tell the two apart. A
    genuine timeout returns `ClaudeAuthStatus(logged_in=False,
    timed_out=True)` instead of None -- see that field's own docstring.

    `env` (H11b critical finding 2): the environment THIS subprocess runs
    in -- defaults to `providers.config.cc_child_env(os.environ)`, the
    SAME stripped env the real `cc:` subprocess gets, so an ambient
    ANTHROPIC_API_KEY/BASE_URL never makes this call (and therefore
    doctor/`_preflight_cc`) report an `authMethod` the actual `cc:` turn
    would never see or use.

    Test seam: `BRIDGE_TEST_CC_AUTH_STATUS` (a JSON object string, same
    shape `claude auth status` itself prints) short-circuits this without
    spawning anything -- set it to `""` (empty) to simulate "resolvable
    binary, unparseable/empty output" -> logged_in=False."""
    override = os.environ.get("BRIDGE_TEST_CC_AUTH_STATUS")
    if override is not None:
        return _parse_auth_status_json(override)
    try:
        argv = resolve_claude_launch_argv()
    except ClaudeCodeNotFoundError:
        return None
    if env is None:
        from halo_harness.providers.config import cc_child_env
        env = cc_child_env(dict(os.environ))
    try:
        proc = subprocess.run(argv + ["auth", "status"], capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return ClaudeAuthStatus(logged_in=False, timed_out=True)
    except OSError:
        return None
    return _parse_auth_status_json(proc.stdout)


def _parse_auth_status_json(text: str) -> ClaudeAuthStatus:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, dict):
        return ClaudeAuthStatus(logged_in=False, raw={})
    return ClaudeAuthStatus(
        logged_in=bool(data.get("loggedIn")), auth_method=data.get("authMethod"),
        email=data.get("email"), subscription_type=data.get("subscriptionType"),
        version=data.get("claudeCodeVersion") or data.get("version"), raw=data,
    )


# ---------------------------------------------------------------------------
# 1.0.1 fixpass finding 1: a cached read of claude_auth_status(), so
# Controller.list_models() (rebuilt on every `/model` open) never spawns a
# `claude auth status` subprocess (up to a 10s timeout) itself -- measured
# live, that subprocess start was the single largest piece of a 2-5s TUI
# freeze on the Kali work box. Only a STARTUP WORKER (tui/app.py's
# on_mount) ever calls `refresh_cached_claude_auth_status` (the one
# function here that actually spawns anything); `list_models()` only ever
# reads `cached_claude_auth_status()`, a plain dict lookup.
# ---------------------------------------------------------------------------
_AUTH_STATUS_CACHE_LOCK = threading.Lock()
_auth_status_cache: Optional[ClaudeAuthStatus] = None
_auth_status_cached_at: float = 0.0

# "Short TTL" per the fix brief -- not enforced as a hard expiry (a stale
# read is still far better than blocking `/model` on a fresh subprocess),
# just how long `cached_claude_auth_status()` considers its own answer
# fresh enough not to bother a caller that wants to know (`is_stale=`).
CACHED_AUTH_STATUS_TTL_S = 30.0


def refresh_cached_claude_auth_status(*, timeout: float = 10.0) -> Optional[ClaudeAuthStatus]:
    """The ONLY function in this cache pair that actually spawns `claude
    auth status` -- always call this OFF the UI thread (a startup worker,
    same convention as `_git_branch_worker`/`catalog_auto_refresh_worker`).
    Stores the result (even `None`, "claude" not found at all) for
    `cached_claude_auth_status()` to read back, and returns it directly too
    so a caller that already IS on a worker thread can use the fresh value
    without a second round trip."""
    global _auth_status_cache, _auth_status_cached_at
    status = claude_auth_status(timeout=timeout)
    with _AUTH_STATUS_CACHE_LOCK:
        _auth_status_cache = status
        _auth_status_cached_at = time.monotonic()
    return status


def cached_claude_auth_status() -> Optional[ClaudeAuthStatus]:
    """Read-only, NEVER spawns a subprocess -- `Controller.list_models()`'s
    own cc: group check. `None` both before the startup worker's first
    `refresh_cached_claude_auth_status()` call has landed AND for "claude"
    genuinely not found -- callers already treat both as "no subscription
    route available" identically (see `list_models()`'s own `cc_available`
    check), so a `/model` opened in the brief window before that first
    refresh lands just shows no cc: group that one time, rather than
    blocking to find out.

    Test seam: `BRIDGE_TEST_CC_AUTH_STATUS` bypasses the cache ENTIRELY
    (calling `claude_auth_status()` directly, which itself reads that same
    var -- already instant, no subprocess) so a test that sets/changes it
    and calls `list_models()` right after always sees its OWN current
    value, never a previous test's cached one left over in this same
    process."""
    if os.environ.get("BRIDGE_TEST_CC_AUTH_STATUS") is not None:
        return claude_auth_status()
    with _AUTH_STATUS_CACHE_LOCK:
        return _auth_status_cache


def cached_auth_status_is_stale(*, max_age: float = CACHED_AUTH_STATUS_TTL_S) -> bool:
    """True when nothing has been cached yet, or the cached answer is older
    than `max_age` -- informational only (nothing in this module acts on
    it automatically); a caller that wants to keep this fresh across a long
    session can check it before deciding to kick off another background
    `refresh_cached_claude_auth_status()`."""
    with _AUTH_STATUS_CACHE_LOCK:
        if _auth_status_cache is None:
            return True
        return (time.monotonic() - _auth_status_cached_at) >= max_age


def reset_cached_claude_auth_status() -> None:
    """Test seam: clear the module-level cache between tests (mirrors
    `tools.webfetch.reset_cache`'s own convention) -- without this, one
    test's cached status would silently leak into the next one's."""
    global _auth_status_cache, _auth_status_cached_at
    with _AUTH_STATUS_CACHE_LOCK:
        _auth_status_cache = None
        _auth_status_cached_at = 0.0


def default_bare_alias_route(*, api_key: Optional[str] = None,
                              status: Optional[ClaudeAuthStatus] = None) -> str:
    """Part A: a bare alias (`--model opus`, no prefix) resolves to
    `"cc"` when `claude auth status` reports a login AND no
    ANTHROPIC_API_KEY is set, to `"ant"` when the key IS set (a deliberate
    key always wins over an incidental subscription login -- never
    silently overridden), else `"none"` (the caller raises, naming both
    options)."""
    if api_key is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return "ant"
    if status is None:
        status = claude_auth_status()
    if status is not None and status.logged_in:
        return "cc"
    return "none"


# ---- profile fields (model.py builds the actual ModelProfile -- see its
# own D10-pattern comment on _profile_from_vendored_databricks_entry, the
# same "plain dict, no dataclass import here" shape, to avoid a
# model.py <-> cc_models.py import cycle) --------------------------------

def profile_fields_for_cc_model(model_id: str) -> Optional[dict]:
    """Plain-dict profile fields for a `cc:`/`ant:` model id/alias-word,
    consulting the `--refresh`-cached catalog (fresher) before the static
    CC_MODEL_TABLE seed above. None when `model_id` (after stripping an
    optional [1m] suffix) isn't one of the nine known names/ids."""
    base, _ = _split_1m_suffix(model_id)
    cached = _load_cc_models_cache().get("profiles", {}).get(base)
    if isinstance(cached, dict):
        return cached
    return CC_MODEL_TABLE.get(base)


# ---- refresh cache (halo models --cc [--refresh]) ------------------

def _cc_models_cache_path(state_dir: Optional[Path] = None) -> Path:
    if state_dir is None:
        from halo_harness.config.paths import bridge_home
        state_dir = bridge_home()
    return Path(state_dir) / "cc-models.json"


def _load_cc_models_cache(state_dir: Optional[Path] = None) -> dict:
    try:
        text = _cc_models_cache_path(state_dir).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _cached_ant_alias(bare_name: str) -> Optional[str]:
    ant_targets = _load_cc_models_cache().get("ant_aliases", {})
    value = ant_targets.get(bare_name)
    return value if isinstance(value, str) and value else None


def refresh_cc_catalog(*, state_dir: Optional[Path] = None, timeout: float = 30.0) -> dict:
    """`halo models --cc --refresh`: pings each of the nine bare
    aliases with a cheap `-p --max-turns 1 --tools "" --strict-mcp-config`
    call, reads the REAL canonical id back from the result JSON's
    `modelUsage` key, and caches `{"ant_aliases": {name: canonical_id},
    "profiles": {}}` to `<state_dir>/cc-models.json`. Costs nine tiny
    subscription calls -- never run implicitly, only from this explicit
    CLI action (token-thrift: a normal session never pays this). Returns
    the freshly written dict; best-effort per-alias (a ping that errors
    just keeps that name's existing/static mapping, never aborts the
    whole refresh).

    H11b finding 24: `--no-session-persistence` (never `--session-id
    <uuid4>`) so this never adds nine throwaway "pong" entries to Claude
    Code's own `/resume` picker; the stripped `cc_child_env` (never this
    process's raw env) so an ambient ANTHROPIC_API_KEY can't bill the
    ping instead of the subscription; and the modelUsage entry with the
    most output tokens (never just `next(iter(...))`, which is dict-
    iteration-order, not "the model that actually answered") is what gets
    cached as the canonical id."""
    try:
        argv = resolve_claude_launch_argv()
    except ClaudeCodeNotFoundError:
        return _load_cc_models_cache(state_dir)
    from halo_harness.providers.config import cc_child_env
    env = cc_child_env(dict(os.environ))
    ant_aliases = dict(_load_cc_models_cache(state_dir).get("ant_aliases", {}))
    for name, cc_value in CC_ALIASES.items():
        try:
            proc = subprocess.run(
                argv + ["-p", "--model", cc_value, "--output-format", "json", "--max-turns", "1",
                        "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                        "--no-session-persistence", "--permission-mode", "bypassPermissions",
                        "reply with the single word pong"],
                capture_output=True, text=True, timeout=timeout, env=env,
            )
            data = json.loads(proc.stdout)
            model_usage = data.get("modelUsage") or {}
            if model_usage:
                canonical = max(model_usage, key=lambda k: (model_usage[k] or {}).get("outputTokens", 0) or 0)
                ant_aliases[name] = canonical
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
            continue
    result = {"ant_aliases": ant_aliases, "profiles": {}}
    path = _cc_models_cache_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass
    return result

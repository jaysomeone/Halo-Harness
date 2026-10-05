"""halo_harness.providers.codex_models -- Halo 2.0.3 round 5i part 2: the
`cx:` route's alias table + `codex` binary detection/login-status check,
mirroring `providers.cc_models` for the Codex CLI instead of Claude Code.
See docs/harness/CODEX-RESEARCH.md sections 1-2 for what each piece below
is confirmed/UNCONFIRMED against.

Never reads `~/.codex/auth.json` -- only shells out to `codex login status`
(plain text, not JSON -- confirmed: that subcommand has no `--json` flag)
and treats the exact string `"Logged in using ChatGPT"` as the one
subscription-eligible answer, the same role `SUBSCRIPTION_AUTH_METHODS ==
{"claude.ai"}` plays for `cc:`.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# docs/harness/CODEX-RESEARCH.md section 2: the documented ChatGPT-login
# model set (learn.chatgpt.com/docs/models, 2026-10-05) -- short aliases are
# Halo's own addition (not a Codex convention), mirroring CC_ALIASES' short
# names. Bare pass-through for anything else (a full id typed verbatim, or a
# future model this table doesn't know yet) -- same "superset, never a
# closed enum" contract every other alias table in this codebase has.
CODEX_ALIASES: "dict[str, str]" = {
    "astra": "gpt-6-astra",
    "sol": "gpt-6.1-sol",
    "luna": "gpt-6-luna",
}
CODEX_CHATGPT_MODEL_IDS = tuple(CODEX_ALIASES.values())


def resolve_codex_alias(bare: str) -> str:
    return CODEX_ALIASES.get(bare, bare)


def alias_display_detail(alias: str) -> str:
    resolved = resolve_codex_alias(alias)
    return f"-> {resolved}" if resolved != alias else ""


class CodexNotFoundError(Exception):
    """No `codex` binary could be resolved -- doctor's "install Codex CLI" case."""


def resolve_codex_launch_argv() -> "list[str]":
    """argv PREFIX for launching `codex` (or a test's fake stand-in).
    `HALO_CODEX_EXE`/legacy `BRIDGE_CODEX_EXE`, when set, is shlex-split --
    same deliberate difference from a single-path contract that `cc_models.
    resolve_claude_launch_argv` documents, for the same reason: a test's fake
    binary is a Python script, so this needs a real two-token argv prefix on
    every platform. Falls back to `shutil.which` on `codex`/`codex.exe`/
    `codex.cmd` only -- CODEX-RESEARCH.md section 1: `codex.ps1` is
    deliberately never probed, the same omission `mcp_setup.find_claude_exe`
    already makes for `claude.ps1` (not directly spawnable via
    `subprocess.Popen` without invoking `powershell.exe` itself). Then
    `~/.local/bin/codex[.exe]` (posix installs). Raises CodexNotFoundError
    (never returns None/[]) when nothing resolves."""
    from halo_harness.config.paths import env_compat
    env_val = env_compat("CODEX_EXE")
    if env_val:
        return shlex.split(env_val, posix=True)
    for name in ("codex", "codex.exe", "codex.cmd"):
        found = shutil.which(name)
        if found:
            return [found]
    from halo_harness.config.paths import home
    for rel in ("codex", "codex.exe"):
        candidate = home() / ".local" / "bin" / rel
        if candidate.exists():
            return [str(candidate)]
    raise CodexNotFoundError(
        "codex executable not found (HALO_CODEX_EXE unset; looked on PATH and ~/.local/bin)"
    )


# ---- login status (text, never JSON -- CODEX-RESEARCH.md section 1) ------

# The one marker that means "a real ChatGPT subscription login" -- every
# other known marker below is logged in via something ELSE (an API key,
# Bedrock, an access/personal-access token, workload identity), confirmed by
# reading the installed codex.exe's own string table (never executed).
CODEX_SUBSCRIPTION_MARKER = "Logged in using ChatGPT"
CODEX_KNOWN_LOGIN_MARKERS = (
    CODEX_SUBSCRIPTION_MARKER,
    "Logged in using an API key",
    "Logged in using access token",
    "Logged in using Amazon Bedrock API key",
    "Logged in using Amazon Bedrock AWS access keys",
    "Logged in using personal access token",
    "Logged in using workload identity",
)


@dataclass(frozen=True)
class CodexAuthStatus:
    logged_in: bool
    auth_method: Optional[str] = None  # "chatgpt" | "other:<raw line>" | None
    raw_text: str = ""
    timed_out: bool = False


def _classify_login_line(text: str) -> "tuple[bool, Optional[str]]":
    line = (text or "").strip()
    if not line or "not logged in" in line.lower():
        return False, None
    if CODEX_SUBSCRIPTION_MARKER in line:
        return True, "chatgpt"
    for marker in CODEX_KNOWN_LOGIN_MARKERS[1:]:
        if marker in line:
            return True, f"other:{marker}"
    return True, f"other:{line[:120]}"


def codex_login_status(*, timeout: float = 10.0, env: Optional[dict] = None) -> Optional[CodexAuthStatus]:
    """Runs `codex login status` and classifies its plain-text stdout.
    Returns None only when the binary can't be found/run at all. Test seam:
    `BRIDGE_TEST_CODEX_LOGIN_STATUS` (the literal stdout text `codex login
    status` would print) short-circuits this without spawning anything --
    set it to `"Not logged in"` to simulate a logged-out box."""
    override = os.environ.get("BRIDGE_TEST_CODEX_LOGIN_STATUS")
    if override is not None:
        logged_in, method = _classify_login_line(override)
        return CodexAuthStatus(logged_in=logged_in, auth_method=method, raw_text=override)
    try:
        argv = resolve_codex_launch_argv()
    except CodexNotFoundError:
        return None
    run_env = env if env is not None else dict(os.environ)
    try:
        proc = subprocess.run(argv + ["login", "status"], capture_output=True, text=True,
                               timeout=timeout, env=run_env)
    except subprocess.TimeoutExpired:
        return CodexAuthStatus(logged_in=False, timed_out=True)
    except OSError:
        return None
    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    logged_in, method = _classify_login_line(text)
    return CodexAuthStatus(logged_in=logged_in, auth_method=method, raw_text=text)


# ---- cached read, same TUI-non-blocking reasoning cc_models.py documents -

_AUTH_CACHE_LOCK = threading.Lock()
_auth_cache: Optional[CodexAuthStatus] = None
_auth_cached_at: float = 0.0
CACHED_AUTH_TTL_S = 30.0


def refresh_cached_codex_auth_status(*, timeout: float = 10.0) -> Optional[CodexAuthStatus]:
    """The only function here that may spawn `codex login status` -- call
    off the UI thread (a startup worker), same convention as
    `cc_models.refresh_cached_claude_auth_status`."""
    global _auth_cache, _auth_cached_at
    status = codex_login_status(timeout=timeout)
    with _AUTH_CACHE_LOCK:
        _auth_cache = status
        _auth_cached_at = time.monotonic()
    return status


def cached_codex_auth_status() -> Optional[CodexAuthStatus]:
    """Read-only, never spawns a subprocess. Test seam: like `cc_models`,
    `BRIDGE_TEST_CODEX_LOGIN_STATUS` bypasses the cache entirely so a test
    that changes it mid-run always sees its own current value."""
    if os.environ.get("BRIDGE_TEST_CODEX_LOGIN_STATUS") is not None:
        return codex_login_status()
    with _AUTH_CACHE_LOCK:
        return _auth_cache


def cached_auth_status_is_stale(*, max_age: float = CACHED_AUTH_TTL_S) -> bool:
    """Mirrors `cc_models.cached_auth_status_is_stale` -- True when
    nothing has been cached yet, or the cached answer is older than
    `max_age`; informational only."""
    with _AUTH_CACHE_LOCK:
        if _auth_cache is None:
            return True
        return (time.monotonic() - _auth_cached_at) >= max_age


def reset_cached_codex_auth_status() -> None:
    global _auth_cache, _auth_cached_at
    with _AUTH_CACHE_LOCK:
        _auth_cache = None
        _auth_cached_at = 0.0


def codex_login_available() -> bool:
    """`cached_codex_auth_status()` only -- the picker/enablement's own
    cache-only check, mirroring `init_providers.claude_login_available`."""
    status = cached_codex_auth_status()
    return bool(status and status.logged_in and status.auth_method == "chatgpt")


# ---- model profile fields (reuses the oai: vendored catalog -- see
# CODEX-RESEARCH.md section 2: all four known ids already have real rows
# there from round 5i part 1; Halo never invents context/pricing numbers) --

# A ChatGPT subscription has no metered per-token price -- these three keys
# are always dropped even when the vendored row carries real API pricing
# (that pricing is for the oai: route, which pays per token; cx: does not).
_SUBSCRIPTION_NO_PRICE_KEYS = ("price_in", "price_out", "price_cache_read", "price_cache_write")

# Fallback for a model id/alias this box's vendored catalog doesn't carry
# (a future ChatGPT-plan model Halo hasn't shipped a catalog row for yet) --
# the same figures every KNOWN id currently shares (CODEX-RESEARCH.md
# section 2), not a guess pulled from nowhere.
_UNKNOWN_CODEX_MODEL_DEFAULTS = {"context_tokens": 1_050_000, "max_output_tokens": 128_000}


def profile_fields_for_codex_model(model_id: str) -> dict:
    """Plain-dict `ModelProfile` fields for a `cx:` model id/alias-word --
    reuses `providers.models_dev`'s vendored `openai` fallback (the SAME
    file `oai:` reads) since the underlying model is identical either way,
    only the billing differs. Resolves the alias first so `cx:astra` and
    `cx:gpt-6-astra` look up the same row. Never returns pricing (see
    `_SUBSCRIPTION_NO_PRICE_KEYS`); falls back to `_UNKNOWN_CODEX_MODEL_
    DEFAULTS` for any id the vendored catalog doesn't carry, never an
    empty dict, so a resolver always has SOMETHING better than the bare
    dataclass default to show."""
    from halo_harness.providers.models_dev import load_vendored_openai_fallback, openai_profile_fields_from_models_dev
    resolved = resolve_codex_alias(model_id)
    catalog = load_vendored_openai_fallback()
    entry = catalog.get(resolved)
    if isinstance(entry, dict):
        fields = openai_profile_fields_from_models_dev(entry)
    else:
        fields = dict(_UNKNOWN_CODEX_MODEL_DEFAULTS)
    for key in _SUBSCRIPTION_NO_PRICE_KEYS:
        fields.pop(key, None)
    fields.setdefault("vision", True)
    fields["reasoning"] = "openai"
    return fields


# ---- refresh cache (halo models --cx [--refresh], brief item 1: "confirm
# each by a one-token headless call on /models refresh, mark the refused
# ones") -- mirrors `cc_models.refresh_cc_catalog`'s cache shape, simpler:
# codex's own ids are already exact/canonical (no "latest pointer" like
# cc:'s bare opus/sonnet), so this only ever needs to confirm or refuse
# each one, never re-derive a different canonical id. ------------------

def _cx_models_cache_path(state_dir: Optional[Path] = None) -> Path:
    if state_dir is None:
        from halo_harness.config.paths import bridge_home
        state_dir = bridge_home()
    return Path(state_dir) / "cx-models.json"


def load_cx_models_cache(state_dir: Optional[Path] = None) -> dict:
    """`{"refused": [<alias>, ...], "checked_at": <iso8601 or None>}` --
    `{}` (never a crash) when the cache is missing/unparseable."""
    import json as _json
    try:
        text = _cx_models_cache_path(state_dir).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = _json.loads(text)
    except _json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def refresh_cx_catalog(*, state_dir: Optional[Path] = None, timeout: float = 30.0) -> dict:
    """`halo models --cx --refresh`: a cheap `codex exec --ephemeral`
    one-token ping per known alias, reading back whether the model id was
    accepted at all (an `error`/`turn.failed` event, or a nonzero exit
    with no `thread.started` line, counts as refused) -- never run
    implicitly, only from this explicit CLI action, same token-thrift rule
    `cc_models.refresh_cc_catalog` documents. Best-effort per-alias: a
    ping that errors for an UNRELATED reason (codex itself unreachable,
    timeout) leaves that alias's prior cached answer alone rather than
    marking it refused on a false signal."""
    import json as _json
    try:
        argv = resolve_codex_launch_argv()
    except CodexNotFoundError:
        return load_cx_models_cache(state_dir)
    from halo_harness.providers.config import cc_child_env
    env = cc_child_env(dict(os.environ))
    prior = load_cx_models_cache(state_dir)
    refused = list(prior.get("refused") or [])
    for alias, model_id in CODEX_ALIASES.items():
        try:
            proc = subprocess.run(
                argv + ["exec", "--ephemeral", "--skip-git-repo-check", "--json", "-m", model_id,
                         "reply with the single word pong"],
                capture_output=True, text=True, timeout=timeout, env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue  # unrelated failure -- leave this alias's prior answer alone
        saw_thread_start = False
        was_refused = False
        for line in (proc.stdout or "").splitlines():
            try:
                obj = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if obj.get("type") == "thread.started":
                saw_thread_start = True
            if obj.get("type") in ("error", "turn.failed"):
                was_refused = True
        if not saw_thread_start and proc.returncode != 0:
            was_refused = True
        if was_refused and alias not in refused:
            refused.append(alias)
        elif not was_refused and alias in refused:
            refused.remove(alias)
    import time as _time
    result = {"refused": refused, "checked_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())}
    path = _cx_models_cache_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass
    return result


def installed_codex_version() -> Optional[str]:
    try:
        argv = resolve_codex_launch_argv()
    except CodexNotFoundError:
        return None
    try:
        proc = subprocess.run(argv + ["--version"], capture_output=True, text=True, timeout=10.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (proc.stdout or "").strip().split(" ")[-1] or None

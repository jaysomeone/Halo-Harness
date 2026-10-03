"""halo_harness.providers.cx_models -- the `cx:` route's model and login
layer: the installed `codex` binary (OpenAI's Codex CLI), driven headlessly
under the user's own ChatGPT subscription login (Plus, Pro, Team,
Enterprise, Edu). The Codex counterpart of `providers/cc_models.py`.

Halo never reads `~/.codex/auth.json` or any OAuth token: login state comes
from `codex login status`, and the model list, plan and usage windows come
from the binary's own app-server (`model/list`, `account/read`,
`account/rateLimits/read`), so what the picker shows is exactly what this
account may use. Verified live against codex-cli 0.153.4 on 2026-10-02
(`cx_tested.json`).

The catalog is cached at `<state_dir>/cx-models.json` by `refresh_cx_catalog`
(`halo models --cx --refresh`, `/models refresh`, and the TUI's startup
worker when the cache is empty). `SEED_MODELS` below is only the fallback
for a box that has never refreshed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CX_PREFIX = "cx:"
SUBSCRIPTION_METHOD = "chatgpt"
DEFAULT_CONTEXT_TOKENS = 258_400   # `thread/tokenUsage/updated.modelContextWindow`, observed live
DEFAULT_MAX_OUTPUT_TOKENS = 128_000

# Fallback only (a fresh box before its first refresh); `model/list` on a
# ChatGPT team login, 2026-10-02.
SEED_MODELS = (
    {"id": "gpt-6-astra", "display_name": "GPT-6-Astra", "is_default": True, "default_effort": "low",
     "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"]},
    {"id": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol", "default_effort": "medium",
     "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"]},
    {"id": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra", "default_effort": "medium",
     "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"]},
    {"id": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "default_effort": "medium",
     "efforts": ["low", "medium", "high", "xhigh", "max"]},
    {"id": "gpt-5.5", "display_name": "GPT-5.5", "default_effort": "xhigh",
     "efforts": ["low", "medium", "high", "xhigh"]},
)

# Keys that would make the child bill an API key instead of the ChatGPT
# login, or point it at a different endpoint.
_CX_STRIP_KEYS = frozenset({"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
                             "OPENAI_ORGANIZATION", "OPENAI_PROJECT_ID"})


class CodexNotFoundError(Exception):
    pass


def resolve_codex_launch_argv() -> "list[str]":
    """argv prefix for the codex binary. `HALO_CODEX_EXE` (or the legacy
    `BRIDGE_`/`ROLO_CLAUDE_` spellings) is shlex-split, the same contract as
    `cc_models.resolve_claude_launch_argv`, so a test can point it at
    `"<python>" "<fake script>"`."""
    import shlex

    from halo_harness.config.paths import env_compat, home
    env_val = env_compat("CODEX_EXE")
    if env_val:
        return shlex.split(env_val, posix=True)
    for name in ("codex", "codex.cmd", "codex.exe"):
        found = shutil.which(name)
        if found:
            return [found]
    for rel in ("codex", "codex.exe"):
        candidate = home() / ".local" / "bin" / rel
        if candidate.exists():
            return [str(candidate)]
    raise CodexNotFoundError("codex executable not found (HALO_CODEX_EXE unset; looked on PATH and ~/.local/bin)")


def codex_installed() -> bool:
    try:
        resolve_codex_launch_argv()
        return True
    except CodexNotFoundError:
        return False


def cx_child_env(env: dict) -> dict:
    """The child env for every `codex` spawn: the ordinary tool-child env
    (harness secrets stripped) minus anything that would switch Codex off
    the ChatGPT login. `CODEX_HOME` is kept, so a custom Codex home works."""
    from halo_harness.providers.config import tool_child_env
    stripped = tool_child_env(env)
    return {k: v for k, v in stripped.items() if k not in _CX_STRIP_KEYS}


# ---- login status --------------------------------------------------------

@dataclass(frozen=True)
class CodexLoginStatus:
    logged_in: bool
    method: Optional[str] = None      # "chatgpt" | "api_key" | None
    timed_out: bool = False
    text: str = ""


def parse_login_status_text(text: str) -> CodexLoginStatus:
    """`codex login status` prints one line (to stderr on 0.153.4):
    "Logged in using ChatGPT", "Logged in using an API key - sk-...",
    or "Not logged in"."""
    line = (text or "").strip().splitlines()
    line = next((ln.strip() for ln in line if ln.strip().lower().startswith(("logged in", "not logged in"))), "")
    low = line.lower()
    if low.startswith("logged in"):
        method = SUBSCRIPTION_METHOD if "chatgpt" in low else ("api_key" if "api key" in low else "other")
        return CodexLoginStatus(logged_in=True, method=method, text=line)
    return CodexLoginStatus(logged_in=False, text=line)


def codex_login_status(*, timeout: float = 10.0, env: Optional[dict] = None) -> Optional[CodexLoginStatus]:
    """None only when the binary can't be found or run. Test seam:
    `BRIDGE_TEST_CX_LOGIN_STATUS` (the text `codex login status` would
    print) short-circuits without spawning anything."""
    override = os.environ.get("BRIDGE_TEST_CX_LOGIN_STATUS")
    if override is not None:
        return parse_login_status_text(override)
    try:
        argv = resolve_codex_launch_argv()
    except CodexNotFoundError:
        return None
    env = env if env is not None else cx_child_env(dict(os.environ))
    try:
        proc = subprocess.run(argv + ["login", "status"], capture_output=True, text=True, timeout=timeout,
                              env=env, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return CodexLoginStatus(logged_in=False, timed_out=True)
    except OSError:
        return None
    return parse_login_status_text((proc.stdout or "") + "\n" + (proc.stderr or ""))


def is_subscription_login(status: Optional[CodexLoginStatus]) -> bool:
    return bool(status and status.logged_in and status.method == SUBSCRIPTION_METHOD)


# Same cache convention as cc_models' auth-status cache: only a worker
# thread (or headless code with no UI thread) calls `refresh_...`; listing
# surfaces read `cached_...`, which never spawns anything.
_LOGIN_CACHE_LOCK = threading.Lock()
_login_cache: Optional[CodexLoginStatus] = None
_login_cached_at: float = 0.0
CACHED_LOGIN_TTL_S = 30.0


def refresh_cached_codex_login_status(*, timeout: float = 10.0) -> Optional[CodexLoginStatus]:
    global _login_cache, _login_cached_at
    status = codex_login_status(timeout=timeout)
    with _LOGIN_CACHE_LOCK:
        _login_cache = status
        _login_cached_at = time.monotonic()
    return status


def cached_codex_login_status() -> Optional[CodexLoginStatus]:
    if os.environ.get("BRIDGE_TEST_CX_LOGIN_STATUS") is not None:
        return codex_login_status()
    with _LOGIN_CACHE_LOCK:
        return _login_cache


def cached_login_is_stale(*, max_age: float = CACHED_LOGIN_TTL_S) -> bool:
    with _LOGIN_CACHE_LOCK:
        return _login_cache is None or (time.monotonic() - _login_cached_at) >= max_age


def reset_cached_codex_login_status() -> None:
    global _login_cache, _login_cached_at
    with _LOGIN_CACHE_LOCK:
        _login_cache = None
        _login_cached_at = 0.0


def codex_login_available() -> bool:
    """`credentials_present("codex_subscription")`: reads the cache only,
    never spawns (the 2.0.1 launch-hang rule `init_providers.
    claude_login_available` follows). The TUI's startup worker, headless
    mode's background thread, init and doctor prime it."""
    return is_subscription_login(cached_codex_login_status())


# ---- catalog cache (model/list), plan and usage windows ------------------

def _cx_models_cache_path(state_dir: Optional[Path] = None) -> Path:
    if state_dir is None:
        from halo_harness.config.paths import bridge_home
        state_dir = bridge_home()
    return Path(state_dir) / "cx-models.json"


def load_cx_models_cache(state_dir: Optional[Path] = None) -> dict:
    try:
        data = json.loads(_cx_models_cache_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cx_models_cache(data: dict, state_dir: Optional[Path] = None) -> None:
    path = _cx_models_cache_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def cx_models(state_dir: Optional[Path] = None) -> "list[dict]":
    """The account's models: the refreshed cache when there is one, else
    `SEED_MODELS`. Each entry: id, display_name, efforts, default_effort,
    is_default, context_tokens (when Codex has reported one), vision."""
    cached = load_cx_models_cache(state_dir).get("models")
    if isinstance(cached, list) and cached:
        return [m for m in cached if isinstance(m, dict) and m.get("id")]
    return [dict(m) for m in SEED_MODELS]


def cx_catalog_is_seed(state_dir: Optional[Path] = None) -> bool:
    return not load_cx_models_cache(state_dir).get("models")


def _model_entry_from_wire(m: dict) -> dict:
    efforts = [e.get("reasoningEffort") for e in (m.get("supportedReasoningEfforts") or [])
               if isinstance(e, dict) and e.get("reasoningEffort")]
    modalities = [str(x).lower() for x in (m.get("inputModalities") or [])]
    return {
        "id": m.get("model") or m.get("id"), "display_name": m.get("displayName") or m.get("id"),
        "description": m.get("description") or "", "efforts": efforts,
        "default_effort": m.get("defaultReasoningEffort"), "is_default": bool(m.get("isDefault")),
        "vision": ("image" in modalities) if modalities else True,
    }


def resolve_cx_alias(bare: str, state_dir: Optional[Path] = None) -> str:
    """`cx:default` (or a bare `cx:`) is the account's default model; any
    other name passes through unchanged (Codex itself rejects an unknown one)."""
    if bare in ("", "default"):
        for m in cx_models(state_dir):
            if m.get("is_default"):
                return m["id"]
        models = cx_models(state_dir)
        return models[0]["id"] if models else "gpt-6-astra"
    return bare


def cx_model_entry(model_id: str, state_dir: Optional[Path] = None) -> Optional[dict]:
    for m in cx_models(state_dir):
        if m.get("id") == model_id:
            return m
    return None


def profile_fields_for_cx_model(model_id: str, state_dir: Optional[Path] = None) -> dict:
    entry = cx_model_entry(model_id, state_dir) or {}
    observed = (load_cx_models_cache(state_dir).get("context_windows") or {}).get(model_id)
    return {
        "context_tokens": observed or entry.get("context_tokens") or DEFAULT_CONTEXT_TOKENS,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "vision": bool(entry.get("vision", True)),
        "efforts": tuple(entry.get("efforts") or ()),
        "default_effort": entry.get("default_effort"),
    }


def record_context_window(model_id: str, tokens: int, state_dir: Optional[Path] = None) -> None:
    """Codex reports `modelContextWindow` with every token-usage update;
    remember it so `/context` and compaction use the real figure."""
    if not model_id or not isinstance(tokens, int) or tokens <= 0:
        return
    data = load_cx_models_cache(state_dir)
    windows = dict(data.get("context_windows") or {})
    if windows.get(model_id) == tokens:
        return
    windows[model_id] = tokens
    data["context_windows"] = windows
    _save_cx_models_cache(data, state_dir)


def record_rate_limits(rate_limits: dict, state_dir: Optional[Path] = None) -> None:
    if not isinstance(rate_limits, dict):
        return
    data = load_cx_models_cache(state_dir)
    data["rate_limits"] = _slim_rate_limits(rate_limits)
    data["rate_limits_at"] = int(time.time())
    _save_cx_models_cache(data, state_dir)


def _slim_rate_limits(rl: dict) -> dict:
    out = {"plan": rl.get("planType")}
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if isinstance(w, dict):
            out[key] = {"used_percent": w.get("usedPercent"), "window_mins": w.get("windowDurationMins"),
                        "resets_at": w.get("resetsAt")}
    if rl.get("rateLimitReachedType"):
        out["reached"] = rl.get("rateLimitReachedType")
    return out


def _window_label(mins) -> str:
    if not isinstance(mins, (int, float)):
        return "window"
    if mins >= 10080 and mins % 10080 == 0:
        return "weekly" if mins == 10080 else f"{int(mins // 10080)}-week"
    if mins >= 1440 and mins % 1440 == 0:
        return f"{int(mins // 1440)}-day"
    if mins >= 60 and mins % 60 == 0:
        return f"{int(mins // 60)} h"
    return f"{int(mins)} min"


def format_rate_limits(slim: Optional[dict], *, now: Optional[float] = None) -> str:
    """One line for `/providers`, doctor and the status card, e.g.
    `team plan · 5 h: 12% used, resets 14:05 · weekly: 40% used, resets Oct 06`."""
    if not isinstance(slim, dict) or not slim:
        return ""
    parts = [f"{slim['plan']} plan"] if slim.get("plan") else []
    now = now if now is not None else time.time()
    for key in ("primary", "secondary"):
        w = slim.get(key)
        if not isinstance(w, dict):
            continue
        used = w.get("used_percent")
        text = f"{_window_label(w.get('window_mins'))}: {used}% used" if used is not None else _window_label(
            w.get("window_mins"))
        resets = w.get("resets_at")
        if isinstance(resets, (int, float)) and resets > now:
            fmt = "%H:%M" if resets - now < 86400 else "%b %d"
            text += f", resets {time.strftime(fmt, time.localtime(resets))}"
        parts.append(text)
    if slim.get("reached"):
        parts.append(f"limit reached ({slim['reached']})")
    return " · ".join(parts)


def cached_rate_limits_line(state_dir: Optional[Path] = None, *, now: Optional[float] = None) -> str:
    """The cached usage line, with "(as of HH:MM)" once it is more than
    5 minutes old: the windows roll, so an old reading can be far off."""
    data = load_cx_models_cache(state_dir)
    line = format_rate_limits(data.get("rate_limits"), now=now)
    at = data.get("rate_limits_at")
    now = now if now is not None else time.time()
    if line and isinstance(at, (int, float)) and now - at > 300:
        line += f" (as of {time.strftime('%H:%M' if now - at < 86400 else '%b %d %H:%M', time.localtime(at))})"
    return line


def refresh_cx_catalog(*, state_dir: Optional[Path] = None, timeout: float = 30.0) -> dict:
    """Starts a short-lived `codex app-server`, reads `model/list`
    (including hidden models, marked), `account/read` (plan type only; the
    email is not stored) and `account/rateLimits/read`, and writes the
    cache. No model call is made, so a refresh spends nothing. Returns the
    written dict; on any failure the existing cache is returned unchanged."""
    from halo_harness.agent.cx_process import CodexAppServer, CodexRpcError
    existing = load_cx_models_cache(state_dir)
    try:
        server = CodexAppServer.start(cwd=Path.home(), env=cx_child_env(dict(os.environ)))
    except (CodexNotFoundError, OSError, CodexRpcError):
        return existing
    try:
        models: list = []
        cursor = None
        for _ in range(10):
            params = {"includeHidden": True}
            if cursor:
                params["cursor"] = cursor
            res = server.request("model/list", params, timeout=timeout)
            for m in res.get("data") or []:
                if isinstance(m, dict):
                    entry = _model_entry_from_wire(m)
                    if m.get("hidden"):
                        entry["hidden"] = True
                    if entry["id"]:
                        models.append(entry)
            cursor = res.get("nextCursor")
            if not cursor:
                break
        account = (server.request("account/read", {}, timeout=timeout) or {}).get("account") or {}
        try:
            rl = (server.request("account/rateLimits/read", None, timeout=timeout) or {}).get("rateLimits")
        except CodexRpcError:
            rl = None
    except (CodexRpcError, OSError):
        return existing
    finally:
        server.close()
    data = dict(existing)
    if models:
        data["models"] = models
    data["account"] = {"type": account.get("type"), "plan": account.get("planType")}
    data["fetched_at"] = int(time.time())
    data["codex_version"] = installed_codex_version()
    if isinstance(rl, dict):
        data["rate_limits"] = _slim_rate_limits(rl)
        data["rate_limits_at"] = int(time.time())
    _save_cx_models_cache(data, state_dir)
    return data


# ---- tested version range ------------------------------------------------

_CX_TESTED_PATH = Path(__file__).resolve().parent / "cx_tested.json"


def load_cx_tested_range() -> dict:
    try:
        with open(_CX_TESTED_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def installed_codex_version() -> Optional[str]:
    """`codex --version` prints `codex-cli 0.153.4`; returns `0.153.4`."""
    try:
        argv = resolve_codex_launch_argv()
    except CodexNotFoundError:
        return None
    try:
        proc = subprocess.run(argv + ["--version"], capture_output=True, text=True, timeout=10.0,
                              env=cx_child_env(dict(os.environ)), encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    tokens = (proc.stdout or "").strip().split()
    return tokens[-1] if tokens else None


def codex_version_outside_tested_range(version: Optional[str], *, tested: Optional[dict] = None) -> bool:
    from halo_harness.providers.cc_models import version_outside_tested_range
    return version_outside_tested_range(version, tested=tested if tested is not None else load_cx_tested_range())


def prime_codex_login_cache() -> None:
    """Staleness-gated refresh for the places that already prime Claude's
    login cache (startup worker, headless background thread, `/providers`,
    `halo providers`, bugreport). Never raises; a box without codex costs
    one PATH lookup."""
    try:
        if cached_login_is_stale() and codex_installed():
            refresh_cached_codex_login_status()
    except Exception:
        pass


CATALOG_MAX_AGE_S = 24 * 3600


def refresh_cx_catalog_if_stale(state_dir: Optional[Path] = None) -> bool:
    """The background auto-refresh rule every other catalog follows:
    refresh when never fetched or older than 24 h. True when it refreshed."""
    fetched = load_cx_models_cache(state_dir).get("fetched_at")
    if isinstance(fetched, (int, float)) and time.time() - fetched < CATALOG_MAX_AGE_S:
        return False
    return bool(refresh_cx_catalog(state_dir=state_dir).get("fetched_at"))


def models_summary_line(state_dir: Optional[Path] = None, *, refresh: bool = False) -> str:
    """`/models [refresh]`'s Codex line (TUI and headless alike)."""
    if refresh:
        data = refresh_cx_catalog(state_dir=state_dir)
        if not data.get("models"):
            return "Codex subscription refresh failed -- see `halo doctor`."
    shown = [m for m in cx_models(state_dir) if not m.get("hidden")]
    fetched = load_cx_models_cache(state_dir).get("fetched_at")
    when = "built-in list, never refreshed" if not fetched else \
        f"last refreshed {(time.time() - fetched) / 3600:.1f}h ago"
    usage = cached_rate_limits_line(state_dir)
    verb = "refreshed" if refresh else "cached"
    return f"Codex subscription {verb}: {len(shown)} model(s) ({when})" + (f"; {usage}" if usage else ".")

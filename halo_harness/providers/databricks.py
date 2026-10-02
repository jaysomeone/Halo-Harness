"""halo_harness.providers.databricks -- Databricks route-candidate
selection, request body allowlisting, the on-disk route/max_tokens cache,
endpoint/model probing, and models.json read/write. Moved out of bridge.py
unchanged in the H0 package split; see wip/SIGNATURES.md's "M4-M6 additions"
section.

See providers/http.py's module docstring for why open_upstream/
UpstreamConnectError are imported LOCALLY (inside probe_databricks_endpoints/
probe_openrouter_models) instead of at module top level: this module and
http.py depend on each other's functions, and both are only used inside
function bodies, so the cross-imports are deferred to avoid a circular
import at module-load time.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Optional

# 1.0.1 part 2 fixpass finding 11: a quick catalog probe must never inherit
# `open_upstream`'s own 300s idle timeout (meant for a long SSE chat
# stream) -- a stalled read on a catalog fetch would otherwise hang a
# normal quit behind it. Applied via `conn.sock.settimeout(...)` right
# after `open_upstream` connects, in every catalog probe in this module.
_CATALOG_READ_TIMEOUT_S = 30

# A non-blocking lock per provider catalog (so a concurrent refresh -- the
# launch worker, a headless session-start thread, another `/model` open --
# collapses to ONE actual network probe instead of several racing at once)
# plus the monotonic time of the last FAILED attempt (so an auto-refresh
# backs off for `_AUTO_REFRESH_BACKOFF_S` after a failure instead of
# retrying a down/VPN-less host on every single `/model` open, each paying
# the full connect timeout). An explicit, user-requested refresh (`force=
# True` -- `/models refresh`, `/dbx`, `halo models --refresh`) is
# never subject to the backoff, only to the single-flight lock.
_dbx_refresh_lock = threading.Lock()

# 2.0.1 finding 24: the exact wording shown whenever a caller loses one of
# the three catalog-refresh single-flight locks (Databricks/OpenRouter/
# Anthropic) -- shown AS-IS, never dressed up as "refresh failed" (a lock a
# CONCURRENT refresh already holds is not a failure; the caller just needs
# to try again in a moment). `CATALOG_REFRESH_BUSY` is the matching
# sentinel for the two bool-returning refreshers (OpenRouter/Anthropic,
# which have no note string of their own to carry this) -- falsy (`bool()`
# is False) so every existing `if ok:`/`if not ok:` caller keeps treating
# it the same as a plain `False` failure; only a caller that wants the
# distinction checks `is CATALOG_REFRESH_BUSY` (or, for Databricks, compares
# the returned note to `REFRESH_BUSY_NOTE` verbatim).
REFRESH_BUSY_NOTE = "a refresh is already running, try again in a second"


class _RefreshBusy:
    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "CATALOG_REFRESH_BUSY"


CATALOG_REFRESH_BUSY = _RefreshBusy()
_dbx_last_failure_at: "dict[str, float]" = {}  # keyed by str(state_dir) -- see refresh_dbx_catalog
_or_refresh_lock = threading.Lock()
_or_last_failure_at: "dict[str, float]" = {}
_AUTO_REFRESH_BACKOFF_S = 300.0


def _atomic_write_json(path: Path, data) -> None:
    """Write `data` as JSON to `path` via a same-directory tmp file +
    `os.replace` (atomic on both POSIX and Windows for a same-filesystem
    rename) -- a reader (another thread, another process) never observes a
    truncated/partial file mid-write, unlike a plain `open(path, "w")`.
    Caller catches `OSError`, same contract every writer here already has."""
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def databricks_route_candidates(model: str) -> list[tuple[str, bool]]:
    """Ordered (path, body_has_model) candidates for a resolved Databricks
    model name (dbx: prefix already stripped). The invocations path uses the
    model name IN FULL, prefix included -- Databricks pay-per-token serving
    endpoint names keep "databricks-" as part of their real, literal name
    (documented form /serving-endpoints/databricks-meta-llama-3-3-70b-instruct
    /invocations; the Claude passthrough route already sends
    "databricks-claude-..." in full). Stripping it 404s on the primary route
    for every such model (finding 8 -- the brief itself was wrong here)."""
    invocations = (f"/serving-endpoints/{model}/invocations", False)
    mlflow = ("/ai-gateway/mlflow/v1/chat/completions", True)
    if model.startswith("system.ai."):
        return [mlflow, invocations]
    return [invocations, mlflow]


def build_databricks_body(oai_body: dict, include_model: bool, model) -> dict:
    """Filter an OpenAI-chat body down to the Databricks-accepted key
    allowlist, adding/omitting 'model'. `reasoning_effort`/`stream_options`
    (scope B/D) are additive: the proxy's own `anthropic_to_openai` never
    sets either key, so widening this allowlist changes nothing for the
    unchanged proxy path -- only the harness's new request builder
    (providers/request.py) ever populates them."""
    allowed = ("messages", "max_tokens", "temperature", "top_p", "stop", "stream",
               "tools", "tool_choice", "reasoning_effort", "stream_options")
    result = {key: oai_body[key] for key in allowed if key in oai_body}
    if include_model:
        result["model"] = model
    return result


def parse_databricks_max_tokens_limit(err_msg: str) -> int | None:
    """Parse a Databricks 400 error naming a max_tokens ceiling, e.g.
    'max_tokens must be <= 4096'. Deliberately anchored on the '<=' ceiling
    wording: a looser 'max_tokens ... (\\d+)' pattern also matches the
    UNRELATED "... exceed context limit: A + B > L" overflow wording (it
    contains the substring "max_tokens" too), misreading A as if it were a
    max_tokens ceiling and triggering a nonsensical clamp-retry instead of
    the real overflow handling (finding 3)."""
    m = re.search(r"max[_ ]?tokens?\D{0,20}?<=\s*(\d+)", err_msg, re.IGNORECASE)
    return int(m.group(1)) if m else None


def databricks_unreachable_response(detail: str) -> tuple[int, dict, dict]:
    """Build the (status, json_body, extra_headers) tuple for a Databricks connect/DNS failure."""
    msg = f"{detail} (are you on the VPN? Databricks is whitelisted)"
    return 502, {"error": {"type": "api_error", "message": msg}}, {"x-should-retry": "false"}


# In-memory mirror of <state_dir>/routes-cache.json: model -> {"route": int, "max_tokens_limit": int|None}
_DBX_ROUTE_CACHE: dict = {}


def _dbx_cache_path(state_dir) -> Path:
    """routes-cache.json path under state_dir."""
    return Path(state_dir) / "routes-cache.json"


def _dbx_cache_load(state_dir) -> dict:
    """Merge routes-cache.json from disk into _DBX_ROUTE_CACHE (in-memory entries win) and return it."""
    path = _dbx_cache_path(state_dir)
    if not path.exists():
        return _DBX_ROUTE_CACHE
    try:
        with open(path, encoding="utf-8") as f:
            disk = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _DBX_ROUTE_CACHE
    if isinstance(disk, dict):
        for model, entry in disk.items():
            if model not in _DBX_ROUTE_CACHE:
                _DBX_ROUTE_CACHE[model] = entry
    return _DBX_ROUTE_CACHE


def _dbx_cache_save(state_dir) -> None:
    """Write _DBX_ROUTE_CACHE to routes-cache.json. Best-effort; swallow OSError."""
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        with open(_dbx_cache_path(state_dir), "w", encoding="utf-8") as f:
            json.dump(_DBX_ROUTE_CACHE, f)
    except OSError:
        pass


def dbx_cache_get_route(model: str, state_dir) -> int | None:
    """Cached candidate index for model, or None."""
    if model not in _DBX_ROUTE_CACHE:
        _dbx_cache_load(state_dir)
    entry = _DBX_ROUTE_CACHE.get(model)
    return entry.get("route") if isinstance(entry, dict) else None


def dbx_cache_set_route(model: str, index: int, state_dir) -> None:
    """Record which candidate index worked for model, update memory + persist to disk."""
    _DBX_ROUTE_CACHE.setdefault(model, {})["route"] = index
    _dbx_cache_save(state_dir)


def dbx_cache_get_max_tokens_limit(model: str, state_dir) -> int | None:
    """Cached max_tokens ceiling for model, or None."""
    if model not in _DBX_ROUTE_CACHE:
        _dbx_cache_load(state_dir)
    entry = _DBX_ROUTE_CACHE.get(model)
    return entry.get("max_tokens_limit") if isinstance(entry, dict) else None


def dbx_cache_set_max_tokens_limit(model: str, limit: int, state_dir) -> None:
    """Record a discovered max_tokens ceiling for model, update memory + persist to disk."""
    _DBX_ROUTE_CACHE.setdefault(model, {})["max_tokens_limit"] = limit
    _dbx_cache_save(state_dir)


def clear_dbx_route_cache(state_dir) -> None:
    """H14 scope D/J: a refreshed `dbx-endpoints.json` can change which
    candidate index is correct for a model (a family's api_types changed,
    or an endpoint that used to be uncached now has a real RULES-table
    order) -- called after every `models --refresh`/`init --preset work`
    catalog write so a stale route index never outlives the catalog it was
    learned against. Self-healing either way (a wrong cached index just
    costs one extra 404 before the real path is found and re-cached), so
    this is a cheap, safe reset, never a correctness requirement on its own."""
    _DBX_ROUTE_CACHE.clear()
    try:
        path = _dbx_cache_path(state_dir)
        if path.exists():
            path.unlink()
    except OSError:
        pass




def probe_databricks_status(root: str, token: str) -> "tuple[int, bytes]":
    """GET <root>/api/2.0/serving-endpoints; returns (status, raw response
    bytes) for ANY status -- the low-level primitive `_probe_databricks_
    endpoints_raw` (200-only, parsed endpoint list) builds on, and what
    `doctor.py`'s own `--work` token-validity check (H14 scope E: 401 vs
    403-IP-access-list vs 403-other vs 404 wording) uses directly for a
    non-200 response's body. `token` may be empty/None -- scope E: "the
    reachability probe still runs without a token, a 401 proves
    reachability" -- in which case no Authorization header is sent at all
    (an empty Bearer value would just be a different kind of bad token,
    not "no token"). Raises UpstreamConnectError on connect/DNS failure --
    1.0.1 hotfix 2: bounded at `open_upstream`'s own default connect
    timeout (<=8s), so a `doctor --work`/catalog-refresh probe against an
    unreachable/unresolvable host fails fast instead of hanging on a
    black-holed DNS lookup."""
    import urllib.parse
    from halo_harness.providers.http import format_connect_error, open_upstream, UpstreamConnectError
    parsed = urllib.parse.urlparse(root)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    path = parsed.path.rstrip("/") + "/api/2.0/serving-endpoints"
    headers = {"Accept-Encoding": "identity"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn = None
    try:
        conn = open_upstream(host, port, tls)
        # finding 11: 30s, never `open_upstream`'s own 300s idle timeout
        # (meant for a long SSE chat stream) -- a stalled read on a quick
        # catalog/status probe must not be able to hang a normal quit.
        if conn.sock:
            conn.sock.settimeout(_CATALOG_READ_TIMEOUT_S)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        raise UpstreamConnectError(format_connect_error(host, e), host=host) from e
    finally:
        # NEW (H9 post-acceptance): a one-shot GET+read-fully-then-done
        # call (unlike stream_completion/stream_anthropic_completion,
        # which keep `conn` alive for the caller to stream from and close
        # it themselves once done) -- nothing ever closed it here, leaking
        # one socket per call until GC (a real ResourceWarning, verified).
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return resp.status, raw


def _probe_databricks_endpoints_raw(root: str, token: str) -> "tuple[int, list[dict]]":
    """GET <root>/api/2.0/serving-endpoints; returns (status, raw endpoint
    dicts). Raises UpstreamConnectError on connect/DNS failure. Shared by
    `probe_databricks_endpoints` (bare names, unchanged proxy contract) and
    `probe_databricks_endpoints_full` (scope I catalog refresh, extended
    fields) so the HTTP call exists exactly once."""
    status, raw = probe_databricks_status(root, token)
    if status == 200:
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            return 200, list(data.get("endpoints", []))
        except (json.JSONDecodeError, ValueError):
            pass
    return status, []


def probe_databricks_endpoints(root: str, token: str) -> tuple[int, list[str]]:
    """GET <root>/api/2.0/serving-endpoints; returns (status, endpoint_names). Raises UpstreamConnectError on connect/DNS failure."""
    status, entries = _probe_databricks_endpoints_raw(root, token)
    return status, [e.get("name", "") for e in entries]


def probe_databricks_endpoints_full(root: str, token: str) -> "tuple[int, list[dict]]":
    """Like `probe_databricks_endpoints` but keeps each endpoint's `name`,
    `task`, `state.ready`, caller `permission_level` (scope I: "Databricks
    endpoints list"), and (H14 scope D) the fields the generic family/
    api_type RULES table (providers/dbx_routing.py) needs to route it
    without a vendored per-model list: `foundation_model.api_types`,
    `foundation_model.name` (the gateway model id for mlflow/cursor --
    NOT derivable by prefixing the endpoint name, per the brief),
    `foundation_model.model_class` (a family hint, when the listing
    supplies one), `endpoint_type`, `ai_gateway_v2_supported`, and any
    `usage_policy` DBU-rate hint (best-effort -- most workspaces won't
    have one; `dbx_routing.format_dbu_cost` degrades to "?" either way)."""
    status, entries = _probe_databricks_endpoints_raw(root, token)
    out = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        state = e.get("state") if isinstance(e.get("state"), dict) else {}
        fm = e.get("foundation_model") if isinstance(e.get("foundation_model"), dict) else {}
        entry = {
            "name": e["name"], "task": e.get("task"),
            "ready": state.get("ready"), "permission_level": e.get("permission_level"),
            "endpoint_type": e.get("endpoint_type"),
            "ai_gateway_v2_supported": e.get("ai_gateway_v2_supported"),
            "api_types": fm.get("api_types") or [],
            "foundation_model_name": fm.get("name"),
            "model_class": fm.get("model_class"),
        }
        usage_policy = e.get("usage_policy")
        if isinstance(usage_policy, dict):
            entry["usage_policy"] = usage_policy
        out.append(entry)
    return status, out


def dbx_endpoints_path(state_dir) -> Path:
    """dbx-endpoints.json path under state_dir (scope I)."""
    return Path(state_dir) / "dbx-endpoints.json"


def write_dbx_endpoints_json(state_dir, endpoints: list) -> None:
    """Write dbx-endpoints.json as {"<name>": {"task", "ready", "permission_level"}, ...}. Best-effort.

    1.0.1 part 2 fixpass finding 11: tmp file + `os.replace` (see
    `_atomic_write_json`) -- a reader catching this file mid-write (a
    concurrent `halo models`, `/model` open, ...) used to be able to
    see a truncated JSON document (read as a spurious "every endpoint
    removed" diff) rather than either the old or the new catalog whole."""
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        out = {e["name"]: {k: v for k, v in e.items() if k != "name"} for e in endpoints if e.get("name")}
        _atomic_write_json(dbx_endpoints_path(state_dir), out)
    except OSError:
        pass


def load_dbx_endpoints_json(state_dir) -> dict:
    """Read dbx-endpoints.json (dict keyed by name); {} if missing/unparseable."""
    path = dbx_endpoints_path(state_dir)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# H14 scope J: catalog diff, staleness, and the shared "refresh if stale"
# helper used by `/models refresh` (TUI, off the UI thread), `halo
# models --refresh`, and the auto-refresh-on-session-start/on-`/model`-open
# path (`databricks.catalog_max_age_hours`, default 24).
# ---------------------------------------------------------------------------

_TRACKED_DIFF_FIELDS = ("api_types", "task", "endpoint_type", "foundation_model_name", "ready")


def diff_dbx_catalog(old: dict, new: dict) -> dict:
    """`{"added": [names], "removed": [names], "changed": [{"name", "fields": [...]}]}`
    between two dbx-endpoints.json-shaped dicts -- compares only the fields
    a route/picker decision actually depends on (`_TRACKED_DIFF_FIELDS`), so
    an unrelated field (`permission_level`, ...) changing doesn't show up as
    noise."""
    old = old or {}
    new = new or {}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = []
    for name in sorted(set(old) & set(new)):
        old_e, new_e = old.get(name) or {}, new.get(name) or {}
        fields = [f for f in _TRACKED_DIFF_FIELDS if old_e.get(f) != new_e.get(f)]
        if fields:
            changed.append({"name": name, "fields": fields})
    return {"added": added, "removed": removed, "changed": changed}


def format_dbx_diff(diff: dict) -> str:
    """One-line diff summary for a TUI notification / CLI print -- "no
    changes" when the diff is empty."""
    parts = []
    if diff.get("added"):
        parts.append(f"+{len(diff['added'])} added ({', '.join(diff['added'][:3])}{'...' if len(diff['added']) > 3 else ''})")
    if diff.get("removed"):
        parts.append(f"-{len(diff['removed'])} removed ({', '.join(diff['removed'][:3])}{'...' if len(diff['removed']) > 3 else ''})")
    if diff.get("changed"):
        parts.append(f"~{len(diff['changed'])} changed")
    return "; ".join(parts) if parts else "no changes"


def dbx_endpoints_cache_is_old_shape(endpoints: dict) -> bool:
    """1.0.1 hotfix 4: True when this `dbx-endpoints.json` was written by a
    version before H14 scope D ever cached `api_types` at all -- checked on
    the KEY being present (`"api_types" in entry`), never on whether the
    list happens to be empty (a workspace can legitimately have an endpoint
    the platform itself reports zero api_types for; that's real data, not a
    migration signal). An old-shape cache silently degrades `chat_route_
    candidates` to invocations-only for EVERY openai-chat-dialect family
    (its own `listed = set(entry.get("api_types") or [])` filters every
    real candidate out when the key is simply missing) -- this is what made
    a stale post-upgrade cache show `path=invocations` for every family and
    "0 chat-capable" in `init`'s own summary even though the endpoints
    genuinely are chat-capable. `{}` (nothing cached yet) is NOT old-shape
    (nothing to migrate)."""
    if not endpoints:
        return False
    return not any(isinstance(e, dict) and "api_types" in e for e in endpoints.values())


def dbx_endpoints_age_seconds(state_dir) -> Optional[float]:
    """Age of dbx-endpoints.json in seconds, or None if never cached."""
    path = dbx_endpoints_path(state_dir)
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def refresh_dbx_catalog(state_dir, host: str, token: str) -> "tuple[bool, dict, str]":
    """One refresh cycle: probe, write the new catalog, clear the now
    possibly-stale route cache (scope D), and diff against whatever was
    cached before. Returns `(ok, diff, note)` -- `ok=False` (offline/403/...)
    leaves the existing cache completely untouched and `note` says why,
    matching scope J: "failures (offline, 403 IP list) keep the cache and
    say so".

    1.0.1 part 2 fixpass finding 11: single-flight -- a non-blocking lock
    shared with `refresh_dbx_catalog_if_stale` means a concurrent caller
    (the launch worker, a headless session-start thread, another `/model`
    open, an explicit `/models refresh`/`/dbx`) never runs a SECOND probe
    while one is already in flight; it just gets `ok=False` back with a
    one-line note, same shape as any other failure, and the cache is left
    exactly as whichever refresh IS running will soon leave it. A failure
    here also records `time.monotonic()` (keyed by `state_dir`, so distinct
    test scratch dirs -- or a real multi-workspace box -- never share one
    another's backoff) for `refresh_dbx_catalog_if_stale`'s own backoff."""
    key = str(state_dir)
    if not _dbx_refresh_lock.acquire(blocking=False):
        return False, {}, REFRESH_BUSY_NOTE
    try:
        old = load_dbx_endpoints_json(state_dir)
        try:
            status, fetched = probe_databricks_endpoints_full(host, token)
        except Exception as e:
            _dbx_last_failure_at[key] = time.monotonic()
            return False, {}, f"refresh failed ({type(e).__name__}: {e}) -- keeping the cached catalog"
        if status != 200:
            _dbx_last_failure_at[key] = time.monotonic()
            return False, {}, f"refresh failed (HTTP {status}) -- keeping the cached catalog"
        _dbx_last_failure_at.pop(key, None)
        write_dbx_endpoints_json(state_dir, fetched)
        clear_dbx_route_cache(state_dir)
        new = load_dbx_endpoints_json(state_dir)
        diff = diff_dbx_catalog(old, new)
        return True, diff, format_dbx_diff(diff)
    finally:
        _dbx_refresh_lock.release()


def refresh_dbx_catalog_if_stale(state_dir, *, max_age_hours: Optional[float] = None,
                                  force: bool = False, env: Optional[dict] = None,
                                  ) -> Optional["tuple[bool, dict, str]"]:
    """Scope J auto-refresh: `None` when nothing needed doing (not stale,
    or Databricks isn't configured) -- else the same `(ok, diff, note)`
    triple `refresh_dbx_catalog` returns. `max_age_hours` defaults to
    `databricks.catalog_max_age_hours` (~/.halo/config.json, itself
    defaulting to 24). Never raises -- an auto-refresh must not be able to
    break session start or opening `/model`.

    `env` (N2c, 1.0.1 final pass): forwarded straight to `resolve_databricks`
    -- `None` (every pre-existing call site, unchanged) means bare
    `os.environ` exactly as before; a caller with a real session's own
    trust-filtered `Settings.effective_env` (the launch-time catalog-refresh
    worker, `/model`'s open, the headless session-start thread) passes it
    here so credentials living only in a settings.json `env` block resolve
    with the SAME trust rules a real turn would apply, instead of this
    background refresh re-deriving a possibly-different answer from bare
    `os.environ`/an untrusted cwd's settings chain on its own.

    1.0.1 hotfix 4 migration: an OLD-SHAPE cache (written before `api_types`
    was ever cached) is always treated as stale here, REGARDLESS of
    `max_age_hours`/`force` -- "an old cache without api_types is refreshed
    on next use when the network is up" (the "next use" being one of THIS
    function's own existing callers: session start, opening `/model` --
    never a bare `/models`/`halo models`, which must stay
    network-free; see catalog_cli.py's own old-shape handling for what a
    bare, no-network read shows in the meantime)."""
    try:
        if max_age_hours is None:
            from halo_harness.theme import get_config_value
            max_age_hours = get_config_value("databricks.catalog_max_age_hours", default=24)
        if dbx_endpoints_cache_is_old_shape(load_dbx_endpoints_json(state_dir)):
            force = True
        age = dbx_endpoints_age_seconds(state_dir)
        # A max age of 0 (or less) means "always refresh". Clamp the measured
        # age at 0: on Windows a just-written file's mtime can land a few
        # milliseconds AFTER time.time(), making `age` slightly negative and
        # `age < 0` wrongly true.
        always = float(max_age_hours) <= 0
        if age is not None:
            age = max(0.0, float(age))
        if not force and not always and age is not None and age < float(max_age_hours) * 3600:
            return None
        # finding 11: a recent FAILURE backs an AUTO-refresh off for
        # `_AUTO_REFRESH_BACKOFF_S` -- an explicit force=True (/models
        # refresh, /dbx, `halo models --refresh`) is never subject
        # to this, only to the single-flight lock inside refresh_dbx_catalog
        # itself. Without this, a down/VPN-less host got re-probed (full
        # connect timeout apiece) on every single `/model` open.
        #
        # M1 (1.0.1 final pass): `time.monotonic()` counts from OS boot, not
        # from this process's own start -- `.get(key, 0.0)` made the first
        # `_AUTO_REFRESH_BACKOFF_S` after boot look like "a failure just
        # happened at t=0" for a state_dir that has never actually failed.
        # `None` (nothing recorded) now skips the backoff check entirely.
        last_failure_at = _dbx_last_failure_at.get(str(state_dir))
        if not force and last_failure_at is not None and (time.monotonic() - last_failure_at) < _AUTO_REFRESH_BACKOFF_S:
            return None
        from halo_harness.providers.config import resolve_databricks
        dbx = resolve_databricks(env)
        if dbx is None:
            return None
        return refresh_dbx_catalog(state_dir, dbx.host, dbx.token)
    except Exception as e:
        return False, {}, f"auto-refresh errored ({type(e).__name__}: {e}) -- keeping the cached catalog"


def probe_openrouter_models(base_url: str, api_key: str) -> list[dict]:
    """GET {base_url}/models; returns a list of dicts with 'id',
    'context_length', 'max_output_tokens' (the proxy's own resolve_profile
    reads only these two, unchanged), plus -- when OpenRouter's response
    includes them -- 'input_modalities' (from architecture.input_modalities),
    'supported_parameters', and 'pricing' (H0: halo_harness.model's
    ModelProfile reads these three for vision/reasoning/price). Raises
    UpstreamConnectError on connect/DNS failure -- 1.0.1 hotfix 2: bounded
    at `open_upstream`'s own default connect timeout (<=8s)."""
    import urllib.parse
    from halo_harness.providers.http import format_connect_error, open_upstream, UpstreamConnectError
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    path = parsed.path.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}", "Accept-Encoding": "identity"}
    conn = None
    try:
        conn = open_upstream(host, port, tls)
        # finding 11: see probe_databricks_status's own comment.
        if conn.sock:
            conn.sock.settimeout(_CATALOG_READ_TIMEOUT_S)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        raise UpstreamConnectError(format_connect_error(host, e), host=host) from e
    finally:
        # NEW (H9 post-acceptance): see _probe_databricks_endpoints_raw's
        # own comment -- same one-shot leak, same fix.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if resp.status != 200:
        raise RuntimeError(f"OpenRouter /models returned {resp.status}")
    data = json.loads(raw.decode("utf-8", "replace"))
    models = []
    for entry in data.get("data", []):
        model_dict = {
            "id": entry.get("id"),
            "context_length": entry.get("context_length"),
            "max_output_tokens": (entry.get("top_provider") or {}).get("max_completion_tokens"),
        }
        architecture = entry.get("architecture")
        if isinstance(architecture, dict) and architecture.get("input_modalities") is not None:
            model_dict["input_modalities"] = architecture.get("input_modalities")
        if entry.get("supported_parameters") is not None:
            model_dict["supported_parameters"] = entry.get("supported_parameters")
        if entry.get("pricing") is not None:
            model_dict["pricing"] = entry.get("pricing")
        models.append(model_dict)
    return models


def models_json_path(state_dir) -> Path:
    """models.json path under state_dir."""
    return Path(state_dir) / "models.json"


def write_models_json(state_dir, models: list[dict]) -> None:
    """Write models.json as {"<id>": {"context_length":..., "max_output_tokens":...,
    [input_modalities], [supported_parameters], [pricing]}, ...}. The three
    bracketed keys are included only when the probe actually returned them
    (H0: extended probe_openrouter_models) -- the proxy's own resolve_profile
    only ever reads the first two keys, so their presence is harmless to it.
    Best-effort; swallows OSError."""
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        out = {}
        for m in models:
            mid = m.get("id")
            if not mid:
                continue
            entry = {"context_length": m.get("context_length"), "max_output_tokens": m.get("max_output_tokens")}
            for extra_key in ("input_modalities", "supported_parameters", "pricing"):
                if extra_key in m:
                    entry[extra_key] = m[extra_key]
            out[mid] = entry
        _atomic_write_json(models_json_path(state_dir), out)  # finding 11: tmp file + os.replace
    except OSError:
        pass


def load_models_json(state_dir) -> dict:
    """Read models.json (dict keyed by id); {} if missing/unparseable."""
    path = models_json_path(state_dir)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def models_json_age_seconds(state_dir) -> Optional[float]:
    """Age of models.json in seconds, or None if never cached -- the
    OpenRouter mirror of `dbx_endpoints_age_seconds`."""
    path = models_json_path(state_dir)
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def refresh_openrouter_catalog_if_stale(state_dir, *, max_age_hours: Optional[float] = None,
                                         force: bool = False, env: Optional[dict] = None) -> Optional[bool]:
    """H15 part 2 addendum: the OpenRouter mirror of
    `refresh_dbx_catalog_if_stale` -- `None` when nothing needed doing
    (not stale, or OpenRouter isn't configured), else `True`/`False` for
    whether the refresh actually succeeded. Never raises -- same "must not
    break session start/`/model`" contract. `max_age_hours` defaults to
    `databricks.catalog_max_age_hours` too (one shared staleness knob,
    never a second config key for the identical 24h default).

    `env` (N2c, 1.0.1 final pass): forwarded to `resolve_openrouter` --
    see `refresh_dbx_catalog_if_stale`'s own docstring for the full
    rationale (`None` keeps every pre-existing call site unchanged).

    1.0.1 part 2 fixpass finding 11: single-flight (a non-blocking lock --
    a concurrent caller just gets `False` back, same as any other failure,
    while whichever refresh IS already running updates the cache for
    everyone shortly) plus a post-failure backoff for AUTO (`force=False`)
    callers, same shape as `refresh_dbx_catalog`/`refresh_dbx_catalog_if_
    stale`'s own pair -- keyed by `state_dir` so distinct test scratch dirs
    never share one another's backoff."""
    key = str(state_dir)
    try:
        if max_age_hours is None:
            from halo_harness.theme import get_config_value
            max_age_hours = get_config_value("databricks.catalog_max_age_hours", default=24)
        age = models_json_age_seconds(state_dir)
        always = float(max_age_hours) <= 0
        if age is not None:
            age = max(0.0, float(age))
        if not force and not always and age is not None and age < float(max_age_hours) * 3600:
            return None
        # M1: see refresh_dbx_catalog_if_stale's own comment -- None (never
        # recorded) must never read as "a failure just happened at t=0".
        last_failure_at = _or_last_failure_at.get(key)
        if not force and last_failure_at is not None and (time.monotonic() - last_failure_at) < _AUTO_REFRESH_BACKOFF_S:
            return None
        from halo_harness.providers.config import resolve_openrouter
        orc = resolve_openrouter(env)
        if orc is None:
            return None
        if not _or_refresh_lock.acquire(blocking=False):
            # 2.0.1 finding 24: distinguishable from a genuine failure --
            # see CATALOG_REFRESH_BUSY's own module-level docstring.
            return CATALOG_REFRESH_BUSY
        try:
            fetched = probe_openrouter_models(orc.base_url, orc.api_key)
            write_models_json(state_dir, fetched)
            _or_last_failure_at.pop(key, None)
            return True
        except Exception:
            _or_last_failure_at[key] = time.monotonic()
            return False
        finally:
            _or_refresh_lock.release()
    except Exception:
        return False



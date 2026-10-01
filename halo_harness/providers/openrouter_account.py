"""halo_harness.providers.openrouter_account -- H15 part 2 addendum 4
(corrected against OpenRouter's real OpenAPI spec): the OpenRouter
account-balance figure shown in the status bar/`/cost`/`/providers`.
OpenRouter only, the one provider with a balance API this round.

Two DIFFERENT endpoints, two different keys: `GET /api/v1/key` -- the
ordinary `OPENROUTER_API_KEY`: `label`, `usage` (this key's own spend),
`limit` (null when unlimited), `limit_remaining`, `is_free_tier`,
`rate_limit`. `GET /api/v1/credits` -- requires a separate, higher-
privilege `OPENROUTER_MANAGEMENT_KEY` (never the ordinary key): the WHOLE
account's `total_credits`/`total_usage`. Only called when that key is
configured; the ordinary key never reaches `/credits` and the management
key never reaches `/key` or anywhere else.

Segment text, in preference order: (1) `OR $12.40 left` -- `limit_
remaining` from `/key`, when THIS key has a real limit set; (2) `OR $12.40
left` -- the whole account's remaining credits from `/credits`, when a
management key is configured; (3) `OR $3.21 used` -- this key's own
`usage` (a SPEND, not a remaining balance -- the honest fallback for an
unlimited key with no management key).

Every fetch below is best-effort and NEVER raises into a turn: a failure
returns `None` plus one debug log line."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("halo_harness.providers.openrouter_account")

# A balance check is a quick, secondary poll -- never worth `open_upstream`'s
# own 300s post-connect idle timeout (meant for a long SSE chat stream).
_READ_TIMEOUT_S = 10.0


@dataclass
class OrCredits:
    total_credits: float
    total_usage: float

    @property
    def remaining(self) -> float:
        return self.total_credits - self.total_usage


def is_openrouter_official_host(base_url: str) -> bool:
    """H15 part 2 fixpass finding 9: `refresh_cached_openrouter_balance`
    sends BOTH keys -- the ordinary `OPENROUTER_API_KEY` to `/key`, the
    separate, higher-privilege `OPENROUTER_MANAGEMENT_KEY` to `/credits` --
    to whatever `base_url` resolves to, including the documented
    `HALO_OPENROUTER_BASE_URL` (legacy `BRIDGE_OPENROUTER_BASE_URL`) self-hosted-proxy override (possibly plain
    http). True ONLY for the real `https://openrouter.ai/...` host -- a
    self-hosted proxy, a typo, or a plain-http override must never receive
    either key.

    `BRIDGE_TEST_OPENROUTER_HOST_OVERRIDE=1` is a test-only seam (never set
    in production, never documented as a real configuration knob) that lets
    this suite's own mock-server-based tests (`tests/test_h15_openrouter_
    balance.py` and others, all pointed at a loopback `MockGetEndpoints`
    standing in for openrouter.ai) simulate "yes, trust this host" without
    weakening the real check for an actual `HALO_OPENROUTER_BASE_URL`/`BRIDGE_OPENROUTER_BASE_URL` in
    the wild.

    M2 (1.0.1 final pass): the seam is honoured ONLY when `base_url`'s own
    host is actually loopback (`127.0.0.1`/`localhost`/`::1`) -- a real
    `HALO_OPENROUTER_BASE_URL`/`BRIDGE_OPENROUTER_BASE_URL` pointed at some OTHER host (e.g. a leaked/
    misconfigured `BRIDGE_TEST_OPENROUTER_HOST_OVERRIDE=1` in a real
    environment, or just a typo) must still be refused outright, never
    waved through on the env var's say-so alone."""
    import os
    import urllib.parse
    try:
        parsed = urllib.parse.urlparse(base_url)
    except ValueError:
        return False
    if (os.environ.get("BRIDGE_TEST_OPENROUTER_HOST_OVERRIDE") == "1"
            and parsed.hostname in ("127.0.0.1", "localhost", "::1")):
        return True
    return parsed.scheme == "https" and parsed.hostname == "openrouter.ai"


@dataclass
class OrKeyInfo:
    label: Optional[str]
    limit: Optional[float]
    usage: Optional[float]
    limit_remaining: Optional[float]
    is_free_tier: bool
    rate_limit: Optional[dict] = None


def _get(base_url: str, api_key: str, path: str) -> Optional[dict]:
    """Shared `GET {base_url}{path}` -- returns the parsed `data` object (or
    `None` on any failure). `api_key` is sent ONLY as this one call's own
    bearer token -- `fetch_credits`/`fetch_key_info` below each pass the
    RIGHT key in; they must never cross over."""
    import urllib.parse
    from halo_harness import __version__
    from halo_harness.providers.http import open_upstream

    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    full_path = parsed.path.rstrip("/") + path
    headers = {"Authorization": f"Bearer {api_key}", "Accept-Encoding": "identity",
               "User-Agent": f"halo/{__version__}"}
    conn = None
    try:
        conn = open_upstream(host, port, tls)
        if conn.sock:
            conn.sock.settimeout(_READ_TIMEOUT_S)
        conn.request("GET", full_path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            log.debug("openrouter_account: %s returned %s", path, resp.status)
            return None
        data = json.loads(raw.decode("utf-8", "replace"))
        return data.get("data") if isinstance(data, dict) else None
    except Exception as e:
        log.debug("openrouter_account: %s failed: %s: %s", path, type(e).__name__, e)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def fetch_credits(base_url: str, management_key: str) -> Optional[OrCredits]:
    """`GET /credits`, with the MANAGEMENT key. `None` on any failure. Never
    call this with the ordinary `OPENROUTER_API_KEY` -- OpenRouter's own
    spec requires a Management key here."""
    data = _get(base_url, management_key, "/credits")
    if data is None:
        return None
    try:
        return OrCredits(total_credits=float(data.get("total_credits") or 0),
                          total_usage=float(data.get("total_usage") or 0))
    except (TypeError, ValueError) as e:
        log.debug("openrouter_account: /credits unparseable: %s", e)
        return None


def fetch_key_info(base_url: str, api_key: str) -> Optional[OrKeyInfo]:
    """`GET /key` (NOT `/auth/key`, which does not exist), with the
    ORDINARY inference key. `None` on any failure."""
    data = _get(base_url, api_key, "/key")
    if data is None:
        return None

    def _num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    rate_limit = data.get("rate_limit")
    return OrKeyInfo(label=data.get("label"), limit=_num(data.get("limit")), usage=_num(data.get("usage")),
                      limit_remaining=_num(data.get("limit_remaining")), is_free_tier=bool(data.get("is_free_tier")),
                      rate_limit=rate_limit if isinstance(rate_limit, dict) else None)


def resolve_balance(key_info: Optional[OrKeyInfo], credits: Optional[OrCredits]) -> Optional[dict]:
    """`{"amount": float, "kind": "limit_remaining"|"credits"|"usage"}` per
    the module docstring's own 3-way order -- `None` when nothing is
    available at all."""
    if key_info is not None and key_info.limit is not None and key_info.limit_remaining is not None:
        return {"amount": key_info.limit_remaining, "kind": "limit_remaining"}
    if credits is not None:
        return {"amount": credits.remaining, "kind": "credits"}
    if key_info is not None and key_info.usage is not None:
        return {"amount": key_info.usage, "kind": "usage"}
    return None


# In-memory cache -- same cache/lock/TTL convention `providers/cc_models.py`'s
# own claude-auth-status cache already uses.
_BALANCE_CACHE_LOCK = threading.Lock()
_balance_cache: "Optional[dict]" = None  # {"amount", "kind", "label", "fetched_at", "fetched_at_wall"}
_balance_last_attempt_at: float = 0.0

#: "again every 5 minutes" (the addendum's own cadence).
BALANCE_REFRESH_INTERVAL_S = 300.0
#: "dimmed when the figure is older than 10 minutes" (status bar's own rule).
BALANCE_STALE_AFTER_S = 600.0
#: "after every turn that used an OpenRouter route (debounced to at most
#: once per 60s)".
BALANCE_POST_TURN_DEBOUNCE_S = 60.0


def refresh_cached_openrouter_balance(base_url: str, api_key: str, *,
                                       management_key: Optional[str] = None) -> Optional[dict]:
    """Fetches `/key` always, `/credits` only when `management_key` is
    given -- updates the cache ONLY on success (a failure leaves whatever
    was cached before untouched). Call off the UI thread. `None` when this
    attempt produced nothing (an older cache entry may still remain).

    finding 9: refuses outright (before touching either key) when
    `base_url` isn't the real `openrouter.ai` host -- see `is_openrouter_
    official_host`."""
    if not is_openrouter_official_host(base_url):
        log.debug("refresh_cached_openrouter_balance: base_url %r is not the real openrouter.ai -- "
                  "skipping the fetch, neither key sent", base_url)
        return None
    global _balance_cache, _balance_last_attempt_at
    with _BALANCE_CACHE_LOCK:
        _balance_last_attempt_at = time.monotonic()
    key_info = fetch_key_info(base_url, api_key)
    credits = fetch_credits(base_url, management_key) if management_key else None
    balance = resolve_balance(key_info, credits)
    if balance is None:
        return None
    entry = {
        "amount": balance["amount"], "kind": balance["kind"],
        "label": key_info.label if key_info else None,
        "is_free_tier": key_info.is_free_tier if key_info else False,
        # monotonic for the status bar's own staleness math; wall-clock for
        # the human-readable "as of HH:MM:SS" `/cost`/`/providers` print.
        "fetched_at": time.monotonic(), "fetched_at_wall": time.time(),
    }
    with _BALANCE_CACHE_LOCK:
        _balance_cache = entry
    return entry


def cached_openrouter_balance() -> Optional[dict]:
    """Read-only, never makes a network call."""
    with _BALANCE_CACHE_LOCK:
        return dict(_balance_cache) if _balance_cache is not None else None


_KIND_VERB = {"limit_remaining": "left", "credits": "left", "usage": "used"}
_KIND_NOTE = {
    "limit_remaining": "this key's own limit",
    "credits": "account credits, via the management key",
    "usage": "this key's spend so far -- no limit set, no management key configured",
}


def format_status_bar_segment() -> Optional[str]:
    """`"OR $12.40 left"` / `"OR $3.21 used"` -- the status bar's own
    rendering of the cached balance. `None` (segment omitted entirely) when
    no fetch has ever succeeded."""
    entry = cached_openrouter_balance()
    if entry is None:
        return None
    verb = _KIND_VERB.get(entry["kind"], "left")
    return f"OR ${entry['amount']:.2f} {verb}"


def format_balance_line() -> Optional[str]:
    """`/cost`/`/providers`'s own shared one-line rendering of the cached
    balance -- names WHICH of the three kinds it is, the key label, and the
    time of the reading, same cache `cached_openrouter_balance()` the
    status bar reads. `None` (print nothing) when no fetch has ever
    succeeded."""
    import datetime
    entry = cached_openrouter_balance()
    if entry is None:
        return None
    label = entry.get("label") or "(unlabelled key)"
    when = datetime.datetime.fromtimestamp(entry["fetched_at_wall"]).strftime("%H:%M:%S")
    note = _KIND_NOTE.get(entry["kind"], "")
    verb = "remaining" if entry["kind"] != "usage" else "used so far"
    return f"OpenRouter: ${entry['amount']:.2f} {verb} ({note}; key: {label}, as of {when})"


def openrouter_balance_refresh_due_after_turn() -> bool:
    """The post-turn debounce gate: True once `BALANCE_POST_TURN_DEBOUNCE_S`
    has passed since the last fetch ATTEMPT (success or failure) -- checked
    by the caller (the agent loop, after a turn that used an OpenRouter
    route finishes) before kicking off another background refresh, so a
    chatty multi-turn session doesn't hammer this endpoint once per turn."""
    with _BALANCE_CACHE_LOCK:
        return (time.monotonic() - _balance_last_attempt_at) >= BALANCE_POST_TURN_DEBOUNCE_S


def reset_cached_openrouter_balance() -> None:
    """Test seam: clear the module-level cache between tests (mirrors
    `cc_models.reset_cached_claude_auth_status`'s own convention) -- without
    this, one test's cached balance would silently leak into the next one's
    (a plain module global, unaffected by env-var/BRIDGE_TEST_HOME scoping)."""
    global _balance_cache, _balance_last_attempt_at
    with _BALANCE_CACHE_LOCK:
        _balance_cache = None
        _balance_last_attempt_at = 0.0

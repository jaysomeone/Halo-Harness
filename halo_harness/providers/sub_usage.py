"""halo_harness.providers.sub_usage -- the subscription usage windows the
status bar shows for a `cc:` or `cx:` session ("5h 58% · wk 18%").

Both numbers are ACCOUNT-wide (every client on that subscription counts
toward them), as last reported by the subscription's own CLI:
  * `cc:` -- Claude Code's stream-json `rate_limit_event` (verified live
    against 2.1.288): `rate_limit_info.unifiedWindows.{five_hour,seven_day}
    .{utilization (0-1), resetsAt (epoch s)}`. Recorded here to
    `<state_dir>/cc-usage.json` -- its own file, since `refresh_cc_catalog`
    rewrites `cc-models.json` wholesale.
  * `cx:` -- Codex's `account/rateLimits/updated`, already cached by
    `cx_models.record_rate_limits` (primary = 5 h, secondary = weekly).

claude emits `rate_limit_event` only on a process's FIRST turn, so a
long-lived `cc:` process would show one stale snapshot forever --
`maybe_refresh_cc_usage` re-reads it in the background from `claude -p
/usage` (a local slash command: 0 turns, 0 tokens, ~2 s), only while a
`cc:` model on a claude.ai subscription is in use.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_CC_WINDOWS = {"five_hour": "session", "seven_day": "weekly"}

# How often a busy cc: session re-reads `/usage` (status events stop while
# idle, so an idle session never probes at all).
CC_USAGE_PROBE_INTERVAL_S = 60.0
_CC_USAGE_PROBE_TIMEOUT_S = 30.0


def _cc_usage_path(state_dir: Optional[Path] = None) -> Path:
    if state_dir is None:
        from halo_harness.config.paths import bridge_home
        state_dir = bridge_home()
    return Path(state_dir) / "cc-usage.json"


def load_cc_usage(state_dir: Optional[Path] = None) -> dict:
    try:
        data = json.loads(_cc_usage_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def record_cc_rate_limits(info: dict, state_dir: Optional[Path] = None) -> None:
    """Merges one `rate_limit_info` into the cache, per window -- an event
    that carries only one window (the older single-window shape:
    top-level `rateLimitType` + `utilization`) never blanks the other."""
    if not isinstance(info, dict):
        return
    windows = dict(info.get("unifiedWindows") or {})
    if not windows and info.get("rateLimitType") and info.get("utilization") is not None:
        windows[info["rateLimitType"]] = {"utilization": info.get("utilization"), "resetsAt": info.get("resetsAt")}
    readings = {}
    for wire_key, key in _CC_WINDOWS.items():
        w = windows.get(wire_key)
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            readings[key] = {"used_percent": round(100 * w["utilization"]), "resets_at": w.get("resetsAt")}
    _merge_cc_usage(readings, state_dir)


def _merge_cc_usage(readings: dict, state_dir: Optional[Path] = None) -> None:
    """`{"session"|"weekly": {"used_percent", "resets_at"}}` merged into the
    cache per window -- a window missing from `readings` is kept as-is."""
    if not readings:
        return
    data = load_cc_usage(state_dir)
    data.update(readings)
    data["at"] = int(time.time())
    path = _cc_usage_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


# `claude -p /usage` (verified live against 2.1.288):
#   Current session: 7% used · resets Oct 3, 2:30pm (America/New_York)
#   Current week (all models): 20% used · resets Oct 5, 2am (America/New_York)
_USAGE_LINE_RES = {
    "session": re.compile(r"^Current session:\s*(\d+)% used(?:\s*·\s*resets\s+(.+?))?\s*$", re.M),
    "weekly": re.compile(r"^Current week \(all models\):\s*(\d+)% used(?:\s*·\s*resets\s+(.+?))?\s*$", re.M),
}
_RESETS_RE = re.compile(
    r"^(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),?\s*)?"
    r"(?:(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm))?"
    r"\s*(?:\((?P<tz>[^)]+)\))?$", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _parse_resets(text: Optional[str], now: float) -> Optional[int]:
    """"Oct 3, 2:30pm (America/New_York)" / "2am (...)" -> epoch seconds,
    None when unparseable (the reading still counts; it just can't roll
    over to 0 on its own before the next probe)."""
    m = _RESETS_RE.match((text or "").strip())
    if not m or not (m["mon"] or m["hour"]):
        return None
    tz = None
    if m["tz"]:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(m["tz"])
        except Exception:
            tz = None
    current = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)
    hour = int(m["hour"] or 0) % 12 + (12 if (m["ampm"] or "").lower() == "pm" else 0)
    try:
        dt = current.replace(hour=hour, minute=int(m["minute"] or 0), second=0, microsecond=0)
        if m["mon"]:
            dt = dt.replace(month=_MONTHS.index(m["mon"].lower()) + 1, day=int(m["day"]))
            if dt.timestamp() < now - 86400:  # "Jan 2" read on Dec 30
                dt = dt.replace(year=dt.year + 1)
        elif dt.timestamp() < now:  # time only: the next occurrence
            dt += timedelta(days=1)
    except ValueError:
        return None
    return int(dt.timestamp())


def parse_cc_usage_text(text: str, *, now: Optional[float] = None) -> dict:
    """The `/usage` report -> `_merge_cc_usage` readings ({} when it has no
    usage lines at all, e.g. a non-subscription login)."""
    now = now if now is not None else time.time()
    readings = {}
    for key, rx in _USAGE_LINE_RES.items():
        m = rx.search(text or "")
        if m:
            readings[key] = {"used_percent": int(m.group(1)), "resets_at": _parse_resets(m.group(2), now)}
    return readings


def probe_cc_usage(state_dir: Optional[Path] = None) -> bool:
    """Runs `claude -p /usage` (no hooks/MCP/tools/session file, the same
    stripped env every cc: child gets) and records what it reports. True
    when a reading was recorded."""
    from halo_harness.providers.cc_models import ClaudeCodeNotFoundError, resolve_claude_launch_argv
    from halo_harness.providers.config import cc_child_env
    try:
        argv = resolve_claude_launch_argv() + [
            "-p", "/usage", "--output-format", "json", "--no-session-persistence",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--settings", '{"disableAllHooks":true}', "--tools", "",
        ]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=_CC_USAGE_PROBE_TIMEOUT_S,
                              env=cc_child_env(dict(os.environ)), stdin=subprocess.DEVNULL)
        result = json.loads(proc.stdout).get("result")
    except (ClaudeCodeNotFoundError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError, AttributeError):
        return False
    readings = parse_cc_usage_text(result if isinstance(result, str) else "")
    _merge_cc_usage(readings, state_dir)
    return bool(readings)


def cc_on_subscription() -> bool:
    """True when the cached `claude auth status` is a claude.ai login --
    never spawns anything (None before the TUI's startup worker lands, a
    gateway-driven claude, or an API-key login all read False)."""
    from halo_harness.providers.cc_models import SUBSCRIPTION_AUTH_METHODS, cached_claude_auth_status
    status = cached_claude_auth_status()
    return bool(status and status.logged_in and status.auth_method in SUBSCRIPTION_AUTH_METHODS)


_probe_lock = threading.Lock()
_probe_running = False
_last_probe_at = float("-inf")


def maybe_refresh_cc_usage(provider: str) -> bool:
    """Called on every status event: starts one background `/usage` probe
    when the route is `cc:` on a subscription, none is running, and the
    last one started over `CC_USAGE_PROBE_INTERVAL_S` ago. Its reading
    shows on the NEXT status event. True when a probe was started."""
    global _probe_running, _last_probe_at
    from halo_harness.config.paths import background_net_disabled
    if provider != "cc" or background_net_disabled() or not cc_on_subscription():
        return False
    with _probe_lock:
        now = time.monotonic()
        if _probe_running or now - _last_probe_at < CC_USAGE_PROBE_INTERVAL_S:
            return False
        _probe_running, _last_probe_at = True, now

    def run() -> None:
        global _probe_running
        try:
            probe_cc_usage()
        except Exception:
            pass
        finally:
            with _probe_lock:
                _probe_running = False

    threading.Thread(target=run, daemon=True, name="cc-usage-probe").start()
    return True


def _pct(window, now: float) -> Optional[int]:
    if not isinstance(window, dict) or not isinstance(window.get("used_percent"), (int, float)):
        return None
    resets = window.get("resets_at")
    # A window that has rolled over since the reading started fresh at 0.
    if isinstance(resets, (int, float)) and resets <= now:
        return 0
    return int(window["used_percent"])


def subscription_usage(provider: str, state_dir: Optional[Path] = None, *,
                       now: Optional[float] = None) -> Optional[dict]:
    """`{"session_pct", "weekly_pct"}` (either may be None) for a `cc`/`cx`
    route, None for any other route or before the first reading."""
    now = now if now is not None else time.time()
    if provider == "cc":
        data = load_cc_usage(state_dir)
        session, weekly = data.get("session"), data.get("weekly")
    elif provider == "cx":
        from halo_harness.providers.cx_models import load_cx_models_cache
        rl = load_cx_models_cache(state_dir).get("rate_limits") or {}
        session, weekly = rl.get("primary"), rl.get("secondary")
    else:
        return None
    usage = {"session_pct": _pct(session, now), "weekly_pct": _pct(weekly, now)}
    return usage if any(v is not None for v in usage.values()) else None


def format_usage_segment(usage: Optional[dict]) -> str:
    """"5h 58% · wk 18%" -- blank when there is no reading at all."""
    if not isinstance(usage, dict):
        return ""
    parts = []
    if usage.get("session_pct") is not None:
        parts.append(f"5h {usage['session_pct']}%")
    if usage.get("weekly_pct") is not None:
        parts.append(f"wk {usage['weekly_pct']}%")
    return " · ".join(parts)

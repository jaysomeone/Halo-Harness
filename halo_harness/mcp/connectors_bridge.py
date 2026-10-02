"""halo_harness.mcp.connectors_bridge -- caching, config, eligibility,
status text and permission-rule translation for the claude.ai connectors
bridge (`halo_harness.mcp.connectors` does discovery/parsing itself; this
module is everything built ON TOP of a `list[ConnectorInfo]`). Split out
to keep each file under the house 250-line limit.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from halo_harness.config.paths import background_net_disabled, bridge_home
from halo_harness.mcp.connectors import ConnectorInfo, discover_connectors_now

_CACHE_NAME = "connectors.json"


def cache_path() -> Path:
    return bridge_home() / "mcp" / _CACHE_NAME


def load_cache() -> "tuple[list[ConnectorInfo], Optional[float]]":
    """`(connectors, fetched_at_epoch_or_None)` -- `[]`/`None` for a
    missing or unreadable cache, never raises."""
    path = cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    if not isinstance(data, dict):
        return [], None
    items = [ConnectorInfo.from_dict(d) for d in (data.get("connectors") or []) if isinstance(d, dict)]
    fetched_at = data.get("fetched_at")
    return items, (fetched_at if isinstance(fetched_at, (int, float)) else None)


def save_cache(connectors: "list[ConnectorInfo]") -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"fetched_at": time.time(), "connectors": [c.to_dict() for c in connectors]}
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ---- config (~/.halo/config.json's "connectors" key) -----------------------

def bridge_enabled() -> bool:
    """`connectors.bridge` -- default True (the brief: "default true when a
    claude.ai login is detected"; since a login that ISN'T there also means
    discovery simply finds nothing, defaulting True costs nothing extra and
    needs no separate "detected" bookkeeping)."""
    from halo_harness.theme import get_config_value
    configured = get_config_value("connectors.bridge", default=None)
    return bool(configured) if configured is not None else True


def connector_config(slug: str) -> dict:
    from halo_harness.theme import get_config_value
    val = get_config_value(f"connectors.{slug}", default={})
    return val if isinstance(val, dict) else {}


def connector_enabled(slug: str) -> bool:
    return bool(connector_config(slug).get("enabled", True))


def connector_always_load(slug: str) -> bool:
    return connector_config(slug).get("alwaysLoad") is True


def connector_max_turns(slug: str) -> int:
    v = connector_config(slug).get("max_turns")
    return v if isinstance(v, int) and v > 0 else 4


# ---- eligibility -------------------------------------------------------------

def claude_binary_available() -> bool:
    from halo_harness.mcp_setup import find_claude_exe
    return find_claude_exe() is not None


def _claude_ai_login_known_false() -> bool:
    """True only once we have an ACTUAL cached answer saying this isn't a
    claude.ai login -- used by `unavailable_reason()` (a pure cache read,
    safe to call anywhere, including every test) for display wording
    only. `discovery_eligible()` below is stricter on purpose -- see its
    own docstring."""
    from halo_harness.providers.cc_models import SUBSCRIPTION_AUTH_METHODS, cached_claude_auth_status
    status = cached_claude_auth_status()
    if status is None:
        return False
    return not (status.logged_in and status.auth_method in SUBSCRIPTION_AUTH_METHODS)


def discovery_eligible() -> bool:
    """Gates `ensure_discovered_in_background()` ONLY -- the brief's own
    wording is a POSITIVE requirement ("when `claude` is on PATH AND the
    cached auth status says claude.ai login"), not merely "unless we know
    otherwise": `claude` being merely present on PATH must never by itself
    spawn a real subprocess automatically (every test on a box that
    happens to have a real `claude` installed -- this one included --
    would otherwise do exactly that the first time any session builds,
    racing the suite's own BRIDGE_TEST_HOME/BRIDGE_STATE_DIR scoping).
    `halo mcp list --refresh`/`/mcp` reconnect stay unaffected -- those
    are explicit, user-initiated, bounded calls (`refresh_now()` below),
    never gated by this function at all."""
    if not (bridge_enabled() and claude_binary_available()):
        return False
    from halo_harness.init_providers import claude_login_available
    return claude_login_available()


def unavailable_reason() -> Optional[str]:
    """One line naming why the connectors section is absent/empty, for
    `/mcp`/`halo mcp list`/doctor -- `None` when discovery is expected to
    work (it may still legitimately find zero connectors)."""
    if not bridge_enabled():
        return "claude.ai connectors: disabled (connectors.bridge=false in ~/.halo/config.json)."
    if not claude_binary_available():
        return "claude.ai connectors: `claude` is not installed, so halo cannot discover or bridge them."
    from halo_harness.providers.cc_models import is_claude_gateway_driven
    if is_claude_gateway_driven():
        return ("claude.ai connectors: the installed `claude` is wired to a gateway, not a claude.ai "
                "subscription login -- nothing for the bridge to discover.")
    if _claude_ai_login_known_false():
        return "claude.ai connectors: `claude` is installed but not logged in with a claude.ai subscription."
    return None


# ---- session-level orchestration --------------------------------------------

_session_lock = threading.Lock()
_session_discovered = False


def reset_session_state() -> None:
    """Test seam: a fresh once-per-session flag."""
    global _session_discovered
    with _session_lock:
        _session_discovered = False


def ensure_discovered_in_background(*, timeout: float = 20.0, on_done: "Optional[Callable]" = None) -> bool:
    """Starts real discovery on a daemon thread at most ONCE per process
    (brief: "once per session in a background worker... never on the UI
    thread"). Returns True iff a worker was actually started. Honors
    `BRIDGE_TEST_NO_BACKGROUND_NET=1` like every other startup worker in
    this tree."""
    global _session_discovered
    if background_net_disabled() or not discovery_eligible():
        return False
    with _session_lock:
        if _session_discovered:
            return False
        _session_discovered = True

    def _run() -> None:
        connectors = discover_connectors_now(timeout=timeout)
        save_cache(connectors)
        if on_done is not None:
            try:
                on_done(connectors)
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True, name="halo-connectors-discovery").start()
    return True


def refresh_now(*, timeout: float = 20.0) -> "list[ConnectorInfo]":
    """Synchronous, forced refresh (`halo mcp list --refresh`, `/mcp`
    reconnect) -- ignores the once-per-session gate and any existing cache."""
    connectors = discover_connectors_now(timeout=timeout)
    save_cache(connectors)
    global _session_discovered
    with _session_lock:
        _session_discovered = True
    return connectors


def get_connectors(*, refresh: bool = False) -> "list[ConnectorInfo]":
    if refresh:
        return refresh_now()
    connectors, _fetched_at = load_cache()
    return connectors


# ---- status text + permission-rule translation ------------------------------

def connector_status_entry(info: ConnectorInfo) -> dict:
    """The SAME status-dict shape `McpManager.status()` produces (`name`/
    `type`/`state`/`url`/...), so `mcp_cli.format_mcp_list_line`/
    `status_label` render a connector row with zero vocabulary
    duplication -- `type: "connector"` is the one new tag those two
    already special-case."""
    return {"name": f"connector__{info.slug}", "type": "connector", "state": info.status,
            "url": info.url, "host": info.host, "connector_name": info.name,
            "status_text": info.status_text, "tool_count": len(info.tools)}


def status_line(info: ConnectorInfo) -> str:
    from halo_harness.mcp_cli import format_mcp_list_line
    line = format_mcp_list_line(connector_status_entry(info))
    if info.status == "needs_auth":
        line += (" -- authorize it at claude.ai or inside `claude` with /mcp, then run "
                 "`/mcp` reconnect (or `halo mcp list --refresh`) here; halo never reads "
                 "claude's own credentials file")
    return line


_CLAUDE_AI_RULE_RE = re.compile(r"^mcp__claude_ai_([A-Za-z0-9_]+?)(?:__(\*|[A-Za-z0-9_]+))?$")


def translate_claude_ai_rule(raw: str) -> Optional[str]:
    """One Claude Code settings rule naming an `mcp__claude_ai_<Name>__*`
    tool -> the matching `connector__<slug>` rule (bare for the whole
    connector, `connector__<slug>(<tool>)` for one specific underlying
    tool -- `ConnectorTool.permission_content` matches on exactly that).
    `None` for anything else; the caller only ever ADDS what this returns,
    never removes the original."""
    m = _CLAUDE_AI_RULE_RE.match(raw.strip())
    if not m:
        return None
    token, tool = m.group(1), m.group(2)
    slug = token.lower().strip("_") or "connector"
    if tool is None or tool == "*":
        return f"connector__{slug}"
    return f"connector__{slug}({tool})"


def translate_rules(rules: "list", *, action: str) -> "list":
    """`rules` is a list of ALREADY-PARSED `permissions.Rule` objects
    (`permissions.build_rules_from_settings`'s own return shape, not raw
    strings) -- each one naming an `mcp__claude_ai_<Name>__*` tool gets a
    matching `connector__<slug>` `Rule` APPENDED (never replacing the
    original, which simply never matches anything here since halo has no
    `mcp__claude_ai_*` tool of its own). `action` ("deny"/"ask"/"allow")
    is the list's own action -- a `Rule` carries no action of its own to
    recover it from."""
    from halo_harness.permissions import parse_rule
    out = list(rules)
    existing_raw = {getattr(r, "raw", None) for r in rules}
    for rule in rules:
        raw = getattr(rule, "raw", None)
        if not isinstance(raw, str):
            continue
        translated_text = translate_claude_ai_rule(raw)
        if translated_text is None or translated_text in existing_raw:
            continue
        out.append(parse_rule(translated_text, source=getattr(rule, "source", "") or "connectors_bridge",
                                base_dir=getattr(rule, "base_dir", None), action=action))
        existing_raw.add(translated_text)
    return out

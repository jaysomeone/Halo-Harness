"""Pivot / failover — when a role's primary gateway is in trouble, reroute new work
to a healthy alternate so every agent keeps running instead of crawling.

Resolution order for a role: its configured provider, then `fallbacks[role]` (which
should point at DIFFERENT hosts — e.g. a local model server — not another model on
the same overloaded gateway). We pick the first gateway that isn't circuit-open; if
every option is open, we fall back to the least-cooled one and let the governor
pace it.

Config is a plain dict:

    {
        "providers": {                       # name -> entry (base_url, and optional
            "openrouter": {...},             #   rate_rps / burst / max_inflight)
            "local": {...},
        },
        "roles":     {"worker": "openrouter"},
        "fallbacks": {"worker": ["local"]},
    }
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from . import governor


def _chain(config: Dict[str, Any], role: str) -> List[str]:
    providers = config.get("providers") or {}
    primary = (config.get("roles") or {}).get(role)
    fallbacks = (config.get("fallbacks") or {}).get(role) or []
    seen, chain = set(), []
    for name in [primary] + list(fallbacks):
        if name and name in providers and name not in seen:
            seen.add(name)
            chain.append(name)
    return chain


def resolve(config: Dict[str, Any], role: str) -> Tuple[str, Dict[str, Any], bool, str]:
    """Return (provider_name, entry, pivoted, health). `pivoted` is True when we chose
    a fallback because the primary was unhealthy. Raises ValueError when the role has
    neither a configured provider nor a usable fallback."""
    chain = _chain(config, role)
    if not chain:
        raise ValueError("role %r has no configured provider or fallback" % role)
    providers = config.get("providers") or {}
    ranked = []
    for name in chain:
        entry = dict(providers[name])
        entry.setdefault("name", name)
        h = governor.health(governor.key_for(entry))
        ranked.append((name, entry, h))
    for name, entry, h in ranked:
        if h != "open":
            return name, entry, (name != chain[0]), h
    # every gateway is circuit-open: choose the one closest to recovering
    def cooldown(entry: Dict[str, Any]) -> float:
        st = governor.inspect(governor.key_for(entry))
        return st["cooldown_remaining"] if st else 0.0
    name, entry, h = min(ranked, key=lambda r: cooldown(r[1]))
    return name, entry, (name != chain[0]), "open"
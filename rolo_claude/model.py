"""rolo_claude.model -- model reference parsing and profile resolution for
the harness's own agent loop (plan D3). This sits ABOVE the proxy's own
providers.routing.route_model/resolve_profile: `ModelRef` is a richer parse
(keeps the `or:`/`dbx:`/`ant:` prefixes and bare vendor/model or
databricks-*/system.ai.* forms, PLUS a `routes.json` "aliases" table so a
short name like "sonnet" can resolve to a real ref) and `ModelProfile` adds
vision/reasoning/pricing on top of the proxy's plain
context_tokens/max_output_tokens pair -- all sourced from the SAME
models.json + routes.json the proxy already writes/reads
(providers/databricks.py's load_models_json, providers/config.py's
load_routes), so the harness and the proxy never disagree about a model's
capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rolo_claude.providers.databricks import load_models_json
from rolo_claude.providers.routing import InvalidModelError

_ANT_PREFIX = "ant:"
_DBX_PREFIX = "dbx:"
_OR_PREFIX = "or:"
_MAX_ALIAS_HOPS = 4


def _dialect_for(bare_model: str) -> str:
    """Same rule as providers.routing._dbx_dialect: passthrough iff 'claude'
    appears in the (already prefix-stripped) name."""
    return "anthropic-passthrough" if "claude" in bare_model.lower() else "openai-chat"


@dataclass(frozen=True)
class ModelRef:
    raw: str
    provider: str  # "openrouter" | "databricks" | "anthropic"
    model: str  # bare upstream model id/name, dbx:/or:/ant: prefix stripped
    dialect: str  # "openai-chat" | "anthropic-passthrough"


def parse_model_ref(raw: str, routes: Optional[dict] = None) -> ModelRef:
    """Parse a `--model`-style reference. Order: an exact match in
    `routes["aliases"]` is resolved first (recursively, up to
    `_MAX_ALIAS_HOPS` hops, so an alias may point at another alias) --
    everything after that is the same shape providers.routing.route_model
    accepts, minus its header-based tier-alias resolution (x-bridge-main/
    x-bridge-small are an HTTP-proxy-only concept; the harness resolves its
    own main/small refs directly from CLI flags/settings, never via
    headers). Raises InvalidModelError (same exception the proxy's own
    route_model raises, for one consistent vocabulary) when nothing matches.
    """
    routes = routes or {}
    aliases = routes.get("aliases") or {}
    seen = set()
    resolved = raw
    hops = 0
    while resolved in aliases and resolved not in seen and hops < _MAX_ALIAS_HOPS:
        seen.add(resolved)
        resolved = aliases[resolved]
        hops += 1

    if resolved.startswith(_DBX_PREFIX):
        bare = resolved[len(_DBX_PREFIX):]
        return ModelRef(raw=raw, provider="databricks", model=bare, dialect=_dialect_for(bare))
    if resolved.startswith(_OR_PREFIX):
        bare = resolved[len(_OR_PREFIX):]
        return ModelRef(raw=raw, provider="openrouter", model=bare, dialect="openai-chat")
    if resolved.startswith(_ANT_PREFIX):
        bare = resolved[len(_ANT_PREFIX):]
        return ModelRef(raw=raw, provider="anthropic", model=bare, dialect="anthropic-passthrough")
    if "/" in resolved and resolved.count("/") == 1:
        return ModelRef(raw=raw, provider="openrouter", model=resolved, dialect="openai-chat")
    if resolved.startswith("databricks-") or resolved.startswith("system.ai."):
        return ModelRef(raw=raw, provider="databricks", model=resolved, dialect=_dialect_for(resolved))

    raise InvalidModelError(
        f"no route: {raw!r} (accepted forms are dbx:, or:, ant:, vendor/model, "
        f"a bare databricks-*/system.ai.* name, or a routes.json alias)"
    )


@dataclass(frozen=True)
class ModelProfile:
    context_tokens: int = 128000
    max_output_tokens: int = 16384
    vision: bool = False
    reasoning: str = "none"  # "none" | "openai" | "native"
    parallel_tools: bool = True
    reasoning_passback: bool = False
    price_in: Optional[float] = None  # USD per token, prompt side
    price_out: Optional[float] = None  # USD per token, completion side


def _profile_from_models_json_entry(entry: dict) -> ModelProfile:
    context_tokens = entry.get("context_length") or 128000
    max_output_tokens = entry.get("max_output_tokens") or 16384

    vision = False
    modalities = entry.get("input_modalities")
    if isinstance(modalities, list):
        vision = "image" in modalities

    reasoning = "none"
    supported = entry.get("supported_parameters")
    if isinstance(supported, list) and any(p in supported for p in ("reasoning", "include_reasoning")):
        reasoning = "openai"

    price_in = price_out = None
    pricing = entry.get("pricing")
    if isinstance(pricing, dict):
        try:
            price_in = float(pricing["prompt"]) if pricing.get("prompt") is not None else None
        except (TypeError, ValueError):
            price_in = None
        try:
            price_out = float(pricing["completion"]) if pricing.get("completion") is not None else None
        except (TypeError, ValueError):
            price_out = None

    return ModelProfile(
        context_tokens=context_tokens, max_output_tokens=max_output_tokens, vision=vision,
        reasoning=reasoning, price_in=price_in, price_out=price_out,
    )


def resolve_model_profile(ref: ModelRef, state_dir: Path, routes: Optional[dict] = None) -> ModelProfile:
    """models.json (extended probe_openrouter_models data) < routes.json
    ["profiles"][ref.model or "default"] < the ModelProfile dataclass
    defaults. A native Anthropic/Databricks-passthrough ref (dialect ==
    "anthropic-passthrough") gets `reasoning="native"` unless models.json or
    routes.json says otherwise -- a real Claude model always supports
    extended thinking, unlike an openai-chat-dialect model, where reasoning
    support depends on what the upstream actually advertises."""
    routes = routes or {}
    models = load_models_json(state_dir)
    entry = models.get(ref.model)
    if entry:
        profile = _profile_from_models_json_entry(entry)
        if ref.dialect == "anthropic-passthrough" and profile.reasoning == "none":
            profile = ModelProfile(**{**profile.__dict__, "reasoning": "native"})
        return profile

    profiles = routes.get("profiles") or {}
    raw_entry = profiles.get(ref.model) or profiles.get("default")
    if raw_entry:
        return ModelProfile(
            context_tokens=raw_entry.get("context_tokens") or 128000,
            max_output_tokens=raw_entry.get("max_output_tokens") or 16384,
            vision=bool(raw_entry.get("vision", False)),
            reasoning=raw_entry.get("reasoning", "native" if ref.dialect == "anthropic-passthrough" else "none"),
            parallel_tools=bool(raw_entry.get("parallel_tools", True)),
            reasoning_passback=bool(raw_entry.get("reasoning_passback", False)),
            price_in=raw_entry.get("price_in"),
            price_out=raw_entry.get("price_out"),
        )

    if ref.dialect == "anthropic-passthrough":
        return ModelProfile(context_tokens=200000, max_output_tokens=8192, reasoning="native")
    return ModelProfile()


class CostMeter:
    """Accumulates a session's cost across turns. OpenRouter reports actual
    USD cost per response (when the request carried `"usage":{"include":true}`,
    which providers.translate.anthropic_to_openai does not set today -- H4's
    "cost meter" milestone item is what wires that request flag AND reads
    the resulting `usage.cost` field; until then every OpenRouter turn is
    also `None`/unknown, exactly like Databricks, so `has_cost_data` starts
    True and simply never flips to a real number in H0 -- that's expected,
    not a bug). Databricks never reports cost at all ("n/a")."""

    def __init__(self) -> None:
        self.total_usd: float = 0.0
        self.turns: int = 0
        self.has_cost_data: bool = True

    def add_usage(self, provider: str, usage: Optional[dict]) -> Optional[float]:
        """Record one turn's usage; returns this turn's cost in USD, or None
        if unknown/unavailable for this provider or response."""
        self.turns += 1
        if provider == "databricks":
            self.has_cost_data = False
            return None
        cost = usage.get("cost") if isinstance(usage, dict) else None
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            cost = float(cost)
            self.total_usd += cost
            return cost
        self.has_cost_data = False
        return None

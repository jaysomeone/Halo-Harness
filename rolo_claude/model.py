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

# scope J: the home default is the first-party DeepSeek V4 endpoint on
# OpenRouter (verified live against GET /api/v1/models on 2026-09-23/24 --
# `deepseek/deepseek-v4.1-flash` exists and is pinned to the `deepseek`
# provider slug in providers/model_table.json); V3.2 is third-party-only
# now (per the research report) and is kept only as the documented fallback
# when V4.1 Flash isn't reachable.
DEFAULT_MODEL_REF = "or:deepseek/deepseek-v4.1-flash"
FALLBACK_MODEL_REF = "or:deepseek/deepseek-v3.2"


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


def _profile_from_vendored_databricks_entry(entry: dict) -> ModelProfile:
    """H8 scope C: models.dev's own `databricks` provider entry (via
    providers.models_dev.databricks_profile_fields_from_models_dev) into a
    ModelProfile, defaults filling in anything that entry didn't have."""
    from rolo_claude.providers.models_dev import databricks_profile_fields_from_models_dev
    fields = databricks_profile_fields_from_models_dev(entry)
    return ModelProfile(
        context_tokens=fields.get("context_tokens", 128000),
        max_output_tokens=fields.get("max_output_tokens", 16384),
        vision=bool(fields.get("vision", False)),
        reasoning=fields.get("reasoning", "none"),
        price_in=fields.get("price_in"),
        price_out=fields.get("price_out"),
    )


def resolve_model_profile(ref: ModelRef, state_dir: Path, routes: Optional[dict] = None) -> ModelProfile:
    """models.json (extended probe_openrouter_models data) < routes.json
    ["profiles"][ref.model or "default"] < H8 scope C: a vendored fallback
    catalog (providers/catalog/*.json -- OpenRouter's own model list for an
    `openrouter` ref, models.dev's `databricks` provider entry for a
    `databricks` ref) < the ModelProfile dataclass defaults. The vendored
    tier exists so a fresh install with no network yet -- most notably the
    work box, behind a VPN that may not be reachable -- still resolves real
    context/output/pricing/vision for a model this harness ships pinned
    defaults for, instead of the bare dataclass guess. A native Anthropic/
    Databricks-passthrough ref (dialect == "anthropic-passthrough") gets
    `reasoning="native"` unless a more specific source says otherwise -- a
    real Claude model always supports extended thinking, unlike an
    openai-chat-dialect model, where reasoning support depends on what the
    upstream actually advertises."""
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

    # H8 scope C: the vendored fallback tier -- consulted only once neither
    # a live/cached probe NOR an explicit routes.json profile had anything,
    # so it can never override a real probe or a user's own override.
    if ref.provider == "databricks":
        from rolo_claude.providers.models_dev import load_vendored_databricks_fallback
        vendored = load_vendored_databricks_fallback().get(ref.model)
        if vendored:
            profile = _profile_from_vendored_databricks_entry(vendored)
            if ref.dialect == "anthropic-passthrough" and profile.reasoning == "none":
                profile = ModelProfile(**{**profile.__dict__, "reasoning": "native"})
            return profile
    elif ref.provider == "openrouter":
        from rolo_claude.providers.models_dev import load_vendored_openrouter_fallback
        vendored_entry = load_vendored_openrouter_fallback().get(ref.model)
        if vendored_entry:
            return _profile_from_models_json_entry(vendored_entry)

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
    not a bug). Databricks never reports cost at all ("n/a").

    H5 scope D: when a response has no `usage.cost` (an OpenRouter reply
    that genuinely omitted it, or ANY other host/route -- `ant:`, Databricks
    Claude passthrough, a plain openai-chat gateway with no cost field),
    `_fallback_cost` applies OpenCode's own formula (Appendix G) --
    `input*price_in + output*price_out + reasoning*price_out` (reasoning
    billed at the OUTPUT rate; `ModelProfile` has no per-field cache
    pricing to add `cache_read`/`cache_write` as separate line items, so
    those tokens are left priced at the ordinary input rate, folded into
    `input_tokens` -- a documented approximation, not the tiered-pricing/
    `context_over_200k` version of the formula) -- but ONLY when this
    meter was actually constructed with real per-token pricing
    (`price_in`/`price_out`, normally `ModelProfile.price_in`/`price_out`
    from models.json); with neither `usage.cost` nor pricing, behaviour is
    unchanged from before (`has_cost_data` flips False -- "n/a")."""

    def __init__(self, *, price_in: Optional[float] = None, price_out: Optional[float] = None) -> None:
        self.total_usd: float = 0.0
        self.turns: int = 0
        self.has_cost_data: bool = True
        self.price_in = price_in
        self.price_out = price_out

    def _fallback_cost(self, usage) -> Optional[float]:
        if not isinstance(usage, dict) or self.price_in is None or self.price_out is None:
            return None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            return None
        reasoning = usage.get("reasoning_tokens")
        reasoning = reasoning if isinstance(reasoning, int) else 0
        return input_tokens * self.price_in + (output_tokens + reasoning) * self.price_out

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
        fallback = self._fallback_cost(usage)
        if fallback is not None:
            self.total_usd += fallback
            return fallback
        self.has_cost_data = False
        return None

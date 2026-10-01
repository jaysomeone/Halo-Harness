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

import difflib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rolo_claude.providers.databricks import load_dbx_endpoints_json, load_models_json
from rolo_claude.providers.routing import InvalidModelError

_ANT_PREFIX = "ant:"
_DBX_PREFIX = "dbx:"
_OR_PREFIX = "or:"
_CC_PREFIX = "cc:"
_MAX_ALIAS_HOPS = 4

# scope J: the home default is the first-party DeepSeek V4 endpoint on
# OpenRouter (verified live against GET /api/v1/models on 2026-09-23/24 --
# `deepseek/deepseek-v4.1-flash` exists and is pinned to the `deepseek`
# provider slug in providers/model_table.json); V3.2 is third-party-only
# now (per the research report) and is kept only as the documented fallback
# when V4.1 Flash isn't reachable.
DEFAULT_MODEL_REF = "or:deepseek/deepseek-v4.1-flash"
FALLBACK_MODEL_REF = "or:deepseek/deepseek-v3.2"


def resolve_default_model_raw(routes: Optional[dict] = None, env: Optional[dict] = None) -> str:
    """H12 Part A step 3: the ONE place both `-p` and the TUI (`headless.
    build_session` is the single shared session builder both go through)
    and `doctor --work`'s own probe resolve "the configured default model
    when nothing more specific was passed on the command line". Precedence:
    `BRIDGE_MODEL` env var > `routes.json`'s own "default" alias (both
    pre-existing) > `~/.rolo-claude/config.json`'s `"model"` key (written
    by `rolo-claude init`'s step 3 or a plain `rolo-claude config set
    model ...`) > H14 scope C: `dbx:<ANTHROPIC_MODEL>` when the environment
    carries Claude Code's own Databricks work-box shape (`ANTHROPIC_MODEL`/
    `ANTHROPIC_DEFAULT_*_MODEL` resolving a real Databricks config) > the
    hardcoded `DEFAULT_MODEL_REF`. A `--model` flag/arg always wins over ALL
    of this -- callers check that FIRST, same as before this function
    existed (see `build_session`'s own call site).

    `env` (same must-do-6 shape every other resolver here takes) defaults to
    `os.environ`; only the new work-env step reads it (the two pre-existing
    steps keep using `os.environ`/routes.json unchanged, so every existing
    caller that passes no `env` is completely unaffected)."""
    routes = routes or {}
    env_or_routes = os.environ.get("BRIDGE_MODEL") or routes.get("default")
    if env_or_routes:
        return env_or_routes
    from rolo_claude.theme import get_config_value
    configured = get_config_value("model", default=None)
    if isinstance(configured, str) and configured:
        return configured
    env = env if env is not None else os.environ
    from rolo_claude.providers.config import databricks_work_env_active
    if databricks_work_env_active(env):
        anthropic_model = env.get("ANTHROPIC_MODEL")
        if anthropic_model:
            return f"{_DBX_PREFIX}{anthropic_model}"
    return DEFAULT_MODEL_REF


def _cached_dbx_endpoint_names() -> "list[str]":
    """Every endpoint name in `~/.rolo-claude/dbx-endpoints.json` (best
    effort -- `[]` on any error, e.g. no cache yet, so a name-resolution
    check is always a cheap, offline, never-raising local read, exactly
    like `_cache_entry`/`refuse_if_non_chat` already are). 1.0.1 hotfix 6:
    backs BOTH `_is_cached_databricks_endpoint` (a bare name that matches a
    real cached endpoint resolves as Databricks even without the
    `databricks-`/`system.ai.` shape -- a workspace-custom endpoint name
    never has to) and the near-miss suggestion on an unresolvable ref."""
    try:
        from rolo_claude.config.paths import bridge_home
        return list(load_dbx_endpoints_json(bridge_home()))
    except Exception:
        return []


def _is_cached_databricks_endpoint(name: str) -> bool:
    return name in _cached_dbx_endpoint_names()


def _databricks_model_ref(raw: str, bare: str) -> "ModelRef":
    """H14 scope D: the ONE place both `parse_model_ref`'s `dbx:`-prefixed
    and bare `databricks-*`/`system.ai.*` branches build a databricks
    `ModelRef` -- strips a `@anthropic` suffix / applies a `databricks.
    gateway.<endpoint>` override / picks the family's default dialect
    (`providers.dbx_routing.resolve_databricks_dialect`), and refuses a
    known non-chat endpoint (embeddings/whisper) with a clear message
    instead of quietly building a ModelRef nothing can actually drive."""
    from rolo_claude.providers.dbx_routing import refuse_if_non_chat, resolve_databricks_dialect
    clean, dialect = resolve_databricks_dialect(bare)
    err = refuse_if_non_chat(clean)
    if err:
        raise InvalidModelError(err)
    return ModelRef(raw=raw, provider="databricks", model=clean, dialect=dialect)


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

    H11 Part A: `cc:<name>` (the installed `claude` binary, driven under
    the user's own subscription login) and `ant:<name>` (the existing
    native-Anthropic-passthrough provider, now with the SAME nine alias
    names -- fable/opus/opus-5/opus-5.0/opus-4.8/opus-4.6/sonnet/sonnet-5/
    haiku -- resolved to real API ids) both run their bare NAME through
    `providers.cc_models` before falling back to pass-through (a full id,
    or a name that table doesn't know, is untouched -- see
    test_parse_ant_prefix's `ant:claude-opus-4` case). A BARE word with no
    prefix at all naming one of those nine resolves to `cc:`/`ant:`
    depending on what's available (subscription login vs.
    ANTHROPIC_API_KEY) -- see providers.cc_models.default_bare_alias_route.
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
        return _databricks_model_ref(raw, bare)
    if resolved.startswith(_OR_PREFIX):
        bare = resolved[len(_OR_PREFIX):]
        return ModelRef(raw=raw, provider="openrouter", model=bare, dialect="openai-chat")
    if resolved.startswith(_CC_PREFIX):
        from rolo_claude.providers.cc_models import resolve_cc_alias
        bare = resolved[len(_CC_PREFIX):]
        return ModelRef(raw=raw, provider="cc", model=resolve_cc_alias(bare), dialect="cc-subprocess")
    if resolved.startswith(_ANT_PREFIX):
        from rolo_claude.providers.cc_models import resolve_ant_alias
        bare = resolved[len(_ANT_PREFIX):]
        return ModelRef(raw=raw, provider="anthropic", model=resolve_ant_alias(bare), dialect="anthropic-passthrough")
    if "/" in resolved and resolved.count("/") == 1:
        # The bare `vendor/model` OpenRouter form needs BOTH halves: a
        # leading or trailing slash (`/effort`, `vendor/`) or embedded
        # whitespace is a typo, not a model, and used to be accepted here
        # only to fail at request time with an upstream 400.
        vendor, _, model_part = resolved.partition("/")
        if vendor and model_part and not any(ch.isspace() for ch in resolved):
            return ModelRef(raw=raw, provider="openrouter", model=resolved, dialect="openai-chat")
    # 1.0.1 hotfix 6: a bare name resolves to the Databricks route whenever
    # it EITHER matches the generic `databricks-*`/`system.ai.*` shape OR is
    # literally a name in the cached endpoint catalog -- a workspace-custom
    # endpoint (no platform-standard prefix at all, e.g. "my-team-kimi")
    # used to fall straight through to the generic "no route" error below
    # even though `rolo-claude models` already knows it's real and chat-
    # capable.
    if (resolved.startswith("databricks-") or resolved.startswith("system.ai.")
            or _is_cached_databricks_endpoint(resolved)):
        return _databricks_model_ref(raw, resolved)

    from rolo_claude.providers.cc_models import BARE_ALIAS_NAMES, default_bare_alias_route
    if resolved in BARE_ALIAS_NAMES:
        # H14 scope C: at work (Claude Code's own settings env resolves a
        # real Databricks config via ANTHROPIC_MODEL/ANTHROPIC_DEFAULT_*_
        # MODEL), a bare opus/sonnet/haiku maps through THAT env instead of
        # the cc:/ant: subscription route -- checked FIRST, so cc:/ant: only
        # ever apply "when no Databricks env is active" (brief wording).
        from rolo_claude.providers.config import databricks_default_model_for_tier, databricks_work_env_active
        if resolved in ("opus", "sonnet", "haiku") and databricks_work_env_active():
            dbx_model = databricks_default_model_for_tier(resolved)
            if dbx_model:
                return parse_model_ref(f"{_DBX_PREFIX}{dbx_model}", routes)
        route = default_bare_alias_route()
        if route == "cc":
            return parse_model_ref(f"{_CC_PREFIX}{resolved}", routes)
        if route == "ant":
            return parse_model_ref(f"{_ANT_PREFIX}{resolved}", routes)
        raise InvalidModelError(
            f"{resolved!r} needs either a Claude subscription login (run `claude` once to log in, then "
            f"use cc:{resolved}) or ANTHROPIC_API_KEY set (then use ant:{resolved}) -- neither is available"
        )

    # 1.0.1 hotfix 6: "unknown names give a one-line error listing the three
    # closest cached endpoints" -- a bare name that looked like it might be
    # a Databricks endpoint (didn't resolve as anything else, and isn't a
    # vendor/model slash-form meant for OpenRouter) gets a near-miss
    # suggestion sourced from whatever's actually cached, same difflib
    # cutoff `catalog_cli.near_miss_slug` uses elsewhere. Silent (no hint
    # appended) when nothing is cached yet or nothing is close -- never
    # changes the base message's own wording/exit behavior.
    cached_names = _cached_dbx_endpoint_names()
    near = difflib.get_close_matches(raw, cached_names, n=3, cutoff=0.5) if cached_names else []
    hint = f" -- did you mean one of the cached Databricks endpoints: {', '.join(near)}?" if near else ""
    raise InvalidModelError(
        f"no route: {raw!r} (accepted forms are dbx:, or:, ant:, cc:, vendor/model, "
        f"a bare databricks-*/system.ai.* name, a subscription-model alias, or a routes.json alias){hint}"
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
    # H5c Extra (from the H8 must-do list): USD per token for a prompt-cache
    # HIT (cache_read_input_tokens) and a cache-CREATION write
    # (cache_creation_input_tokens) respectively -- distinct from price_in
    # since every real vendor prices these well below the ordinary input
    # rate (a cache read is typically ~10% of price_in; a cache write is
    # typically ~125% of it). None when a source has no such breakdown --
    # CostMeter then falls back to pricing those tokens at the ordinary
    # price_in rate (the pre-H5c approximation), never at $0.
    price_cache_read: Optional[float] = None
    price_cache_write: Optional[float] = None


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

    price_in = price_out = price_cache_read = price_cache_write = None
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
        # H5c Extra: OpenRouter's own `/api/v1/models` pricing object names
        # these two fields `input_cache_read`/`input_cache_write` (same
        # per-token USD units as `prompt`/`completion` -- never per-million).
        try:
            price_cache_read = (float(pricing["input_cache_read"])
                                 if pricing.get("input_cache_read") is not None else None)
        except (TypeError, ValueError):
            price_cache_read = None
        try:
            price_cache_write = (float(pricing["input_cache_write"])
                                  if pricing.get("input_cache_write") is not None else None)
        except (TypeError, ValueError):
            price_cache_write = None

    return ModelProfile(
        context_tokens=context_tokens, max_output_tokens=max_output_tokens, vision=vision,
        reasoning=reasoning, price_in=price_in, price_out=price_out,
        price_cache_read=price_cache_read, price_cache_write=price_cache_write,
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
        price_cache_read=fields.get("price_cache_read"),
        price_cache_write=fields.get("price_cache_write"),
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
    # H11 Part A: `cc:`/`ant:` model_table rows (context/output/pricing for
    # the six subscription models) -- checked FIRST for `cc:` (it has no
    # models.json/routes.json entry of its own at all -- Claude Code is the
    # provider, never OpenRouter/Databricks) and for `ant:` only when the
    # name is one of the nine known ones (anything else keeps falling
    # through to the plain 200000/8192 native-passthrough default below,
    # unchanged from before this milestone).
    if ref.provider == "cc" or (ref.provider == "anthropic" and ref.dialect == "anthropic-passthrough"):
        from rolo_claude.providers.cc_models import profile_fields_for_cc_model
        fields = profile_fields_for_cc_model(ref.model)
        if fields:
            return ModelProfile(
                context_tokens=fields.get("context_tokens", 1_000_000),
                max_output_tokens=fields.get("max_output_tokens", 64_000),
                # H11b finding 7: this call site never read `fields["vision"]`
                # at all (the dataclass default, False, always won) -- every
                # real Claude model accepts image input, so this defaults
                # True even for a future table row that forgets the key.
                vision=bool(fields.get("vision", True)),
                reasoning="native",
                price_in=fields.get("price_in"), price_out=fields.get("price_out"),
                price_cache_read=fields.get("price_cache_read"), price_cache_write=fields.get("price_cache_write"),
            )
        if ref.provider == "cc":
            # An unrecognized cc: model id is still a REAL Claude model
            # (Claude Code resolved it, whatever it is) -- vision=True for
            # the same reason reasoning="native" already is here.
            return ModelProfile(context_tokens=1_000_000, max_output_tokens=64_000, vision=True, reasoning="native")

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
            price_cache_read=raw_entry.get("price_cache_read"),
            price_cache_write=raw_entry.get("price_cache_write"),
        )

    # H8 scope C: the vendored fallback tier -- consulted only once neither
    # a live/cached probe NOR an explicit routes.json profile had anything,
    # so it can never override a real probe or a user's own override.
    #
    # H9 whole-tree review finding 22: for `databricks`, this is now a
    # THREE-step chain, not one lookup -- verified: the vendored package
    # fallback's 30 rows included none of the plan's own work-default
    # models (`databricks-deepseek-v4-1-flash`, `-kimi-k3`, `-glm-5-3`),
    # so all three silently fell all the way through to the bare 128k/16k
    # dataclass guess, triggering auto-compaction at ~90k tokens instead of
    # the real ~773k these models actually support.
    if ref.provider == "databricks":
        from rolo_claude.providers.models_dev import (
            databricks_entries_from_full_models_dev, load_models_dev_json, load_vendored_databricks_fallback,
        )
        # (a) the REFRESHED cache `rolo-claude models --refresh` wrote to
        # <state_dir>/models-dev.json -- fresher than the committed
        # vendored file, and (finding 22) never actually read by anything
        # until now (`load_models_dev_json` had no caller at all).
        refreshed_entry = databricks_entries_from_full_models_dev(load_models_dev_json(state_dir)).get(ref.model)
        vendored = refreshed_entry or load_vendored_databricks_fallback().get(ref.model)
        if vendored:
            profile = _profile_from_vendored_databricks_entry(vendored)
            if ref.dialect == "anthropic-passthrough" and profile.reasoning == "none":
                profile = ModelProfile(**{**profile.__dict__, "reasoning": "native"})
            return profile
        # (b) model_table.json's own per-model row -- built for REQUEST
        # SHAPING (providers/profiles.py's ProviderProfile), not economics,
        # but it DOES already carry a real, hand-verified `context_tokens`/
        # `max_tokens_default` for every one of this harness's own pinned
        # work-default models (e.g. 1,048,576 for all three named above) --
        # a far better answer than the bare dataclass guess below, even
        # though it can't supply pricing/vision.
        from rolo_claude.providers.profiles import load_model_table
        table_entry = (load_model_table().get("databricks") or {}).get(ref.model)
        if isinstance(table_entry, dict) and isinstance(table_entry.get("context_tokens"), int):
            reasoning = "native" if ref.dialect == "anthropic-passthrough" else (
                "native" if table_entry.get("reasoning_effort_supported") else "none")
            return ModelProfile(
                context_tokens=table_entry["context_tokens"],
                max_output_tokens=table_entry.get("max_tokens_cap") or table_entry.get("max_tokens_default")
                or 16384,
                reasoning=reasoning,
            )
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
    not a bug). Databricks never reports a raw `usage.cost` field on the
    wire at all, on any gateway dialect -- but since 1.0.1 hotfix 14, when
    this session's `ModelProfile` resolved a real per-token price for the
    Databricks endpoint (a models.dev `databricks` catalog entry; see
    `resolve_model_profile`'s three-tier chain above), `_fallback_cost`
    below computes a real running cost for it exactly like any other
    provider. Still "n/a" whenever no such price was resolved, which
    remains the common case for most endpoints.

    H5 scope D: when a response has no `usage.cost` (an OpenRouter reply
    that genuinely omitted it, or ANY other host/route -- `ant:`, Databricks
    Claude passthrough, a plain openai-chat gateway with no cost field),
    `_fallback_cost` applies OpenCode's own formula (Appendix G) --
    `input*price_in + output*price_out + reasoning*price_out` (reasoning
    billed at the OUTPUT rate), plus, since H5c, `cache_read*price_cache_read
    + cache_write*price_cache_write` as their OWN line items -- but ONLY
    when this meter was actually constructed with real per-token pricing
    (`price_in`/`price_out`, normally `ModelProfile.price_in`/`price_out`
    from models.json); with neither `usage.cost` nor pricing, behaviour is
    unchanged from before (`has_cost_data` flips False -- "n/a"). H5c Extra
    (from the H8 must-do list): `price_cache_read`/`price_cache_write` are
    optional independently of `price_in`/`price_out` -- a source with plain
    input/output pricing but no cache breakdown (most vendored/fallback
    rows) still gets a real fallback cost, with cache tokens folded into
    the ordinary `price_in` rate (never $0 -- the documented approximation
    this replaces only when the SPECIFIC cache rate isn't known); not the
    tiered-pricing/`context_over_200k` version of the formula."""

    def __init__(self, *, price_in: Optional[float] = None, price_out: Optional[float] = None,
                 price_cache_read: Optional[float] = None, price_cache_write: Optional[float] = None) -> None:
        self.total_usd: float = 0.0
        self.turns: int = 0
        self.has_cost_data: bool = True
        self.price_in = price_in
        self.price_out = price_out
        self.price_cache_read = price_cache_read
        self.price_cache_write = price_cache_write
        # 1.0.1 hotfix 14: cumulative raw token totals, tracked regardless of
        # whether a dollar cost could be computed at all -- the status bar's
        # own "in 12k out 3k" fallback (`model_display.format_status_cost`)
        # when NO price is known for this session's model (the status bar
        # must show something other than a bare "$?").
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0

    def _accumulate_tokens(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        input_tokens = usage.get("input_tokens")
        if isinstance(input_tokens, int):
            self.total_input_tokens += input_tokens
        output_tokens = usage.get("output_tokens")
        if isinstance(output_tokens, int):
            self.total_output_tokens += output_tokens

    def _fallback_cost(self, usage) -> Optional[float]:
        if not isinstance(usage, dict) or self.price_in is None or self.price_out is None:
            return None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            return None
        reasoning = usage.get("reasoning_tokens")
        reasoning = reasoning if isinstance(reasoning, int) else 0
        # H5c Extra: cache_read_input_tokens/cache_creation_input_tokens are
        # SEPARATE fields from input_tokens (see agent/loop.py's own
        # _total_prompt_tokens, which sums all three for context-window
        # accounting) -- before this fix they were silently left out of the
        # fallback formula entirely (not merely mispriced). Billed at their
        # OWN rate when this model's source supplied one, else at the
        # ordinary price_in rate (still billed, just approximated).
        cache_read = usage.get("cache_read_input_tokens")
        cache_read = cache_read if isinstance(cache_read, int) else 0
        cache_write = usage.get("cache_creation_input_tokens")
        cache_write = cache_write if isinstance(cache_write, int) else 0
        cache_read_rate = self.price_cache_read if self.price_cache_read is not None else self.price_in
        cache_write_rate = self.price_cache_write if self.price_cache_write is not None else self.price_in
        return (input_tokens * self.price_in + (output_tokens + reasoning) * self.price_out
                + cache_read * cache_read_rate + cache_write * cache_write_rate)

    def add_usage(self, provider: str, usage: Optional[dict]) -> Optional[float]:
        """Record one turn's usage; returns this turn's cost in USD, or None
        if unknown/unavailable for this provider or response.

        1.0.1 hotfix 14: Databricks used to hard-exit here before ever
        reaching `_fallback_cost` -- "gateway-type-blind, never reports
        cost" -- which made sense back when no per-endpoint Databricks
        price existed anywhere in the harness. Hotfix 12 gave
        `resolve_model_profile` a real per-endpoint price for a Databricks
        model whose name matches a models.dev `databricks` entry (used to
        build THIS meter's own `price_in`/`price_out` at session start), so
        Databricks now flows through the exact same
        `usage["cost"] -> _fallback_cost -> has_cost_data=False` pipeline as
        every other provider: still "n/a" whenever no price was resolved
        (the common case, unchanged), but a real running total whenever one
        was. `provider` is kept as a parameter (some callers still log it)
        but no longer branches here."""
        self.turns += 1
        self._accumulate_tokens(usage)
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

    def add_child_total(self, *, cost_usd: Optional[float], has_cost_data: bool, turns: int = 0) -> None:
        """H9 whole-tree review finding 13: rolls a sub-agent's ALREADY-
        COMPUTED cost total into this (the PARENT's) meter -- adds the
        child's own dollar figure directly rather than re-deriving it via
        `add_usage(provider, usage)`, which would price it against THIS
        meter's own `price_in`/`price_out` (the PARENT's model) even
        though a sub-agent may run an entirely different, differently-
        priced model (`Task(model=...)`/an AgentSpec's own `model:`
        frontmatter) -- re-deriving would silently mis-price every such
        child's spend. `has_cost_data` is ANDed (never OR'd): once any
        part of the session's total spend -- parent or any child -- is
        unknown (e.g. a Databricks child, which never reports cost),
        the COMBINED total is honestly unknown too, not a silent
        under-count presented as a complete figure."""
        self.turns += turns
        if cost_usd is not None:
            self.total_usd += cost_usd
        if not has_cost_data:
            self.has_cost_data = False

"""halo_harness.model -- model reference parsing and profile resolution for
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

from halo_harness.providers.databricks import load_dbx_endpoints_json, load_models_json
from halo_harness.providers.routing import InvalidModelError

_ANT_PREFIX = "ant:"
_DBX_PREFIX = "dbx:"
_OR_PREFIX = "or:"
_CC_PREFIX = "cc:"
_OL_PREFIX = "ol:"
_HF_PREFIX = "hf:"
_HF_ENDPOINT_PREFIX = "endpoint/"
_HF_LOCAL_PREFIX = "local/"
# Halo 2.0.3 round 5f: `hf:mlx/<org>/<repo>` -- a Hub repo served by a
# Halo-managed `mlx_lm.server` (Apple Silicon only; see ModelRef.mlx).
_HF_MLX_PREFIX = "mlx/"
_MAX_ALIAS_HOPS = 4


def is_local_model_ref(ref: "ModelRef") -> bool:
    """Halo 2.0.3 round 5e: True for `ol:` (`provider == "ollama"`),
    `hf:local/*` and `hf:mlx/*` (both carry `provider == "huggingface"` and
    `local=True` -- see `ModelRef.local`'s own docstring: round 5f
    deliberately reuses the SAME flag for mlx) -- never a new "is this
    local" concept of its own. Used by both the hybrid-escalation policy
    (`agent.escalation`) and the saved-vs-cloud cost meter below to decide
    "is this session's model one that policy/meter should even look at"."""
    return ref.provider == "ollama" or (ref.provider == "huggingface" and ref.local)


def catalog_median_prices(*, source_label: bool = False):
    """Halo 2.0.3 round 5e: `(median_price_in, median_price_out)` in USD per
    token -- the saved-vs-cloud reference price when the session has no
    `routing.escalation.to` configured. "the catalog the picker knows"
    (brief wording) is read from the package-vendored fallback catalogs
    (`providers.models_dev.load_vendored_databricks_fallback`/`_openrouter_
    fallback` -- the SAME files `resolve_model_profile`'s own lowest-tier
    fallback already reads), never a live network fetch: computing a
    session's reference price must work exactly the same way under
    `--offline` as it does online, and these files ship with the package
    (always present, no prior `--refresh` required). Today only the
    Databricks fallback carries `cost.input`/`cost.output` fields (the
    OpenRouter one is behavior-only -- temperature/tool_id_format/... --
    with no pricing of its own); both are walked anyway so a future catalog
    update that adds pricing there is picked up with no code change.
    `(None, None)` when neither source has a single priced entry (should
    not happen in practice -- the vendored Databricks file always ships
    with 30+ priced rows -- but never raises either way).

    With `source_label=True`, returns `(median_in, median_out, label)`
    instead, `label` a short phrase for `/cost`'s own breakdown naming how
    many priced models the median was taken over."""
    import statistics
    from halo_harness.providers.models_dev import load_vendored_databricks_fallback, load_vendored_openrouter_fallback
    ins: "list[float]" = []
    outs: "list[float]" = []
    for table in (load_vendored_databricks_fallback(), load_vendored_openrouter_fallback()):
        for entry in (table or {}).values():
            if not isinstance(entry, dict):
                continue
            cost = entry.get("cost") if isinstance(entry.get("cost"), dict) else {}
            ci, co = cost.get("input"), cost.get("output")
            if isinstance(ci, (int, float)) and not isinstance(ci, bool):
                ins.append(ci / 1_000_000)
            if isinstance(co, (int, float)) and not isinstance(co, bool):
                outs.append(co / 1_000_000)
    med_in = statistics.median(ins) if ins else None
    med_out = statistics.median(outs) if outs else None
    if not source_label:
        return med_in, med_out
    label = f"catalog median across {len(ins)} priced model(s) (vendored fallback catalog, no network)"
    return med_in, med_out, label

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
    `HALO_MODEL` (legacy `BRIDGE_MODEL`/`ROLO_CLAUDE_MODEL`, still honoured)
    env var > `routes.json`'s own "default" alias (both
    pre-existing) > `~/.halo/config.json`'s `"model"` key (written
    by `halo init`'s step 3 or a plain `halo config set
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
    from halo_harness.config.paths import env_compat
    routes = routes or {}
    env_or_routes = env_compat("MODEL") or routes.get("default")
    if env_or_routes:
        return env_or_routes
    from halo_harness.theme import get_config_value
    configured = get_config_value("model", default=None)
    if isinstance(configured, str) and configured:
        return configured
    env = env if env is not None else os.environ
    from halo_harness.providers.config import databricks_work_env_active
    if databricks_work_env_active(env):
        anthropic_model = env.get("ANTHROPIC_MODEL")
        if anthropic_model:
            return f"{_DBX_PREFIX}{anthropic_model}"
    return DEFAULT_MODEL_REF


def _cached_dbx_endpoint_names() -> "list[str]":
    """Every endpoint name in `~/.halo/dbx-endpoints.json` (best
    effort -- `[]` on any error, e.g. no cache yet, so a name-resolution
    check is always a cheap, offline, never-raising local read, exactly
    like `_cache_entry`/`refuse_if_non_chat` already are). 1.0.1 hotfix 6:
    backs BOTH `_is_cached_databricks_endpoint` (a bare name that matches a
    real cached endpoint resolves as Databricks even without the
    `databricks-`/`system.ai.` shape -- a workspace-custom endpoint name
    never has to) and the near-miss suggestion on an unresolvable ref."""
    try:
        from halo_harness.config.paths import bridge_home
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
    _refuse_if_disabled("databricks")
    from halo_harness.providers.dbx_routing import refuse_if_non_chat, resolve_databricks_dialect
    clean, dialect = resolve_databricks_dialect(bare)
    err = refuse_if_non_chat(clean)
    if err:
        raise InvalidModelError(err)
    return ModelRef(raw=raw, provider="databricks", model=clean, dialect=dialect)


def _refuse_if_disabled(provider_key: str) -> None:
    """H15 item 21.3: a hand-typed ref whose provider resolves fine but
    isn't ENABLED is refused with the same one-line message `/model`'s own
    dim hint uses for a detected-but-disabled provider -- never silently
    allowed just because credentials happen to be present (item 21.1:
    detected credentials/a claude.ai login never enable anything on their
    own). A box with no `providers` block at all (every pre-H15 install,
    and most existing tests) is unaffected -- `is_provider_disabled_
    message` fails open in that case."""
    from halo_harness.providers.enablement import is_provider_disabled_message
    msg = is_provider_disabled_message(provider_key)
    if msg:
        raise InvalidModelError(msg)


@dataclass(frozen=True)
class ModelRef:
    raw: str
    provider: str  # "openrouter" | "databricks" | "anthropic" | "ollama" | "huggingface"
    model: str  # bare upstream model id/name, dbx:/or:/ant:/ol:/hf: prefix (and ol:'s @host / hf:'s endpoint/<name>) stripped
    dialect: str  # "openai-chat" | "anthropic-passthrough" | "cc-subprocess" | "ollama"
    # Halo 2.0.3 round 2: the `@<hostname>` part of `ol:<model>@<hostname>`
    # (research doc Q6/Q7) -- which entry of `ollama.hosts` this ref names;
    # `None` means "the default host" (`providers.ollama.resolve_ollama_
    # host(None)`). Halo 2.0.3 round 4 reuses this SAME field for
    # `hf:endpoint/<name>` -- which entry of `huggingface.endpoints` this
    # ref names (`providers.huggingface.resolve_huggingface_endpoint`);
    # `None` for an `hf:<org>/<model>` router ref. Round 5 reuses it a
    # THIRD way for `hf:local/<model>@<name>` -- which entry of
    # `huggingface.local_servers` this ref names (`providers.huggingface.
    # resolve_huggingface_local_server`); `None` for a bare `hf:local/
    # <model>` (the default server -- a configured manual entry, else the
    # first auto-detected one, see `providers.huggingface_local_probe.
    # resolve_local_server`). Always `None` for every other provider.
    host: Optional[str] = None
    # Round 5: True for every `hf:local/*` ref (both the bare and `@<name>`
    # shapes) -- the ONLY way to tell "a local-server ref with no `@name`"
    # (host=None, local=True) apart from "a router ref" (host=None,
    # local=False), since both leave `host` unset. An `hf:endpoint/<name>`
    # ref always has `host` set AND `local=False` -- never both this flag
    # and a dedicated endpoint at once. Always `False` for every other
    # provider/ref shape. Round 5f: also `True` for an `hf:mlx/<repo>` ref
    # (see `mlx` below) -- it rides the SAME hf:local profile/fit/role
    # tiers (`model.resolve_model_profile`, `roles.default_role_for_ref`),
    # since a Halo-managed mlx_lm.server is exactly as "local" as any other
    # `hf:local/*` server.
    local: bool = False
    # Round 5f: `True` for `hf:mlx/<org>/<repo>` only -- `ref.model` is then
    # the bare Hub repo id (e.g. "mlx-community/Qwen2.5-7B-Instruct-4bit"),
    # used VERBATIM as both the round 5c managed-server registry key and
    # the `mlx_lm.server --model` argument. Unlike a generic `hf:local/*`
    # ref (which only ever resolves to whatever is ALREADY running/
    # configured), `ref.mlx=True` tells `headless.build_session`/`doctor_
    # local.py`/`halo local serve` to ENSURE a managed server for this
    # EXACT repo id exists first (starting one, with a plain consent
    # notice, when it doesn't) -- see `providers.huggingface_mlx.
    # ensure_mlx_server`. Always `False` for every other provider/ref
    # shape, including every other `hf:` shape.
    mlx: bool = False


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
        _refuse_if_disabled("openrouter")
        return ModelRef(raw=raw, provider="openrouter", model=bare, dialect="openai-chat")
    if resolved.startswith(_OL_PREFIX):
        # Halo 2.0.3 round 2: `ol:<model>` (the default host) or
        # `ol:<model>@<hostname>` (a named entry in `ollama.hosts` -- a LAN
        # host or an Ollama Cloud entry addressed by whatever name the user
        # gave it in config; research doc section 7/Q7: the native request
        # shape is identical in all three cases, only `host.url`/`api_key`
        # differ). `partition` (not `split`) on the FIRST "@": an Ollama tag
        # itself may contain ":" (`qwen3:30b`) but never "@", so this is
        # unambiguous either way.
        bare = resolved[len(_OL_PREFIX):]
        model_part, _, host_part = bare.partition("@")
        _refuse_if_disabled("ollama")
        return ModelRef(raw=raw, provider="ollama", model=model_part, dialect="ollama", host=host_part or None)
    if resolved.startswith(_HF_PREFIX):
        # Halo 2.0.3 round 4: `hf:<org>/<model>` (optionally `:fastest`/
        # `:cheapest`/`:preferred`/`:<provider>`, passed through VERBATIM in
        # `ref.model` -- research doc section 9: the suffix is part of the
        # wire `model` field itself, never a separate parameter) routed to
        # the Inference Providers router, dialect "openai-chat" so this
        # reuses the SAME request/stream/profile code OpenRouter already
        # has (`providers.profiles.resolve_profile`'s generic openai-chat
        # fallback branch -- "tools supported, reasoning passthrough as
        # OpenRouter does, no host-specific fields" falls out of that
        # branch for free once `route.provider` is neither "openrouter" nor
        # "databricks"). `hf:endpoint/<name>` is the OTHER shape (a
        # dedicated Inference Endpoint, never the router): `host` carries
        # the bare `<name>` -- the SAME field `ol:<model>@<hostname>` uses
        # to name a config entry, reused here rather than adding a second
        # field for the identical concept (headless._resolve_creds checks
        # `ref.host` to decide which of the two credential sources to
        # resolve). An endpoint ref's `model` is also the bare `<name>`
        # (there is no separate model portion in this shape at all -- a
        # dedicated endpoint serves exactly one model, chosen at
        # provisioning time, never per request).
        bare = resolved[len(_HF_PREFIX):]
        _refuse_if_disabled("huggingface")
        if bare.startswith(_HF_ENDPOINT_PREFIX):
            name = bare[len(_HF_ENDPOINT_PREFIX):]
            if not name:
                raise InvalidModelError(
                    f"no route: {raw!r} (hf:endpoint/ needs a <name> naming a huggingface.endpoints entry)")
            return ModelRef(raw=raw, provider="huggingface", model=name, dialect="openai-chat", host=name)
        if bare.startswith(_HF_LOCAL_PREFIX):
            # Round 5: `hf:local/<model>` (the default local server -- a
            # configured `huggingface.local_servers` entry, else the first
            # auto-detected one) or `hf:local/<model>@<name>` (a NAMED
            # manual entry) -- `partition` on the first "@", same
            # unambiguous reasoning `ol:<model>@<hostname>` already uses
            # above (a local server's model id may itself contain "@" or
            # ":" about as often as an Ollama tag contains ":", i.e. never
            # in practice, but partition is still correct either way since
            # a server NAME is never expected to contain "@" itself).
            inner = bare[len(_HF_LOCAL_PREFIX):]
            model_part, _, server_name = inner.partition("@")
            if not model_part:
                raise InvalidModelError(
                    f"no route: {raw!r} (hf:local/ needs a <model>, e.g. hf:local/qwen3-30b or "
                    f"hf:local/qwen3-30b@my-server)")
            return ModelRef(raw=raw, provider="huggingface", model=model_part, dialect="openai-chat",
                             host=server_name or None, local=True)
        if bare.startswith(_HF_MLX_PREFIX):
            # Round 5f: `hf:mlx/<org>/<repo>` -- always parses, on every
            # platform (the one-sentence "MLX runs on Apple Silicon only"
            # refusal happens at RESOLVE time -- `providers.huggingface_mlx.
            # ensure_mlx_server` -- never at parse time, exactly like an
            # `hf:local/<model>` ref parses fine with no server running
            # yet). `repo` needs a real `<org>/<repo>` shape (at least one
            # internal "/", no leading/trailing slash, no whitespace) --
            # mlx_lm's own Hub convention, and the same registry key/
            # `--model` argument this ref resolves to verbatim.
            repo = bare[len(_HF_MLX_PREFIX):]
            if ("/" not in repo or repo.startswith("/") or repo.endswith("/")
                    or any(ch.isspace() for ch in repo)):
                raise InvalidModelError(
                    f"no route: {raw!r} (hf:mlx/ needs a <org>/<repo> Hugging Face Hub id, e.g. "
                    f"hf:mlx/mlx-community/Qwen2.5-7B-Instruct-4bit)")
            return ModelRef(raw=raw, provider="huggingface", model=repo, dialect="openai-chat",
                             host=None, local=True, mlx=True)
        if not bare:
            raise InvalidModelError(
                f"no route: {raw!r} (hf: needs <org>/<model>[:suffix], endpoint/<name>, local/<model>, "
                f"or mlx/<org>/<repo>)")
        return ModelRef(raw=raw, provider="huggingface", model=bare, dialect="openai-chat")
    if resolved.startswith(_CC_PREFIX):
        from halo_harness.providers.cc_models import resolve_cc_alias
        bare = resolved[len(_CC_PREFIX):]
        _refuse_if_disabled("claude_subscription")
        return ModelRef(raw=raw, provider="cc", model=resolve_cc_alias(bare), dialect="cc-subprocess")
    if resolved.startswith(_ANT_PREFIX):
        from halo_harness.providers.cc_models import resolve_ant_alias
        bare = resolved[len(_ANT_PREFIX):]
        _refuse_if_disabled("anthropic")
        return ModelRef(raw=raw, provider="anthropic", model=resolve_ant_alias(bare), dialect="anthropic-passthrough")
    if "/" in resolved and resolved.count("/") == 1:
        # The bare `vendor/model` OpenRouter form needs BOTH halves: a
        # leading or trailing slash (`/effort`, `vendor/`) or embedded
        # whitespace is a typo, not a model, and used to be accepted here
        # only to fail at request time with an upstream 400.
        vendor, _, model_part = resolved.partition("/")
        if vendor and model_part and not any(ch.isspace() for ch in resolved):
            _refuse_if_disabled("openrouter")
            return ModelRef(raw=raw, provider="openrouter", model=resolved, dialect="openai-chat")
    # 1.0.1 hotfix 6: a bare name resolves to the Databricks route whenever
    # it EITHER matches the generic `databricks-*`/`system.ai.*` shape OR is
    # literally a name in the cached endpoint catalog -- a workspace-custom
    # endpoint (no platform-standard prefix at all, e.g. "my-team-kimi")
    # used to fall straight through to the generic "no route" error below
    # even though `halo models` already knows it's real and chat-
    # capable.
    if (resolved.startswith("databricks-") or resolved.startswith("system.ai.")
            or _is_cached_databricks_endpoint(resolved)):
        return _databricks_model_ref(raw, resolved)

    from halo_harness.providers.cc_models import BARE_ALIAS_NAMES, default_bare_alias_route
    if resolved in BARE_ALIAS_NAMES:
        # H14 scope C: at work (Claude Code's own settings env resolves a
        # real Databricks config via ANTHROPIC_MODEL/ANTHROPIC_DEFAULT_*_
        # MODEL), a bare opus/sonnet/haiku maps through THAT env instead of
        # the cc:/ant: subscription route -- checked FIRST, so cc:/ant: only
        # ever apply "when no Databricks env is active" (brief wording).
        from halo_harness.providers.config import databricks_default_model_for_tier, databricks_work_env_active
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
        f"no route: {raw!r} (accepted forms are dbx:, or:, ant:, cc:, ol:, hf:, vendor/model, "
        f"a bare databricks-*/system.ai.* name, a subscription-model alias, or a routes.json alias){hint}"
    )


@dataclass(frozen=True)
class ModelProfile:
    context_tokens: int = 128000
    max_output_tokens: int = 16384
    vision: bool = False
    # W4 unbuilt surfaces (MCP): "audio content from MCP tools passed
    # through as a typed block to models that accept audio, otherwise the
    # existing note" -- same shape/plumbing as `vision` above. False for
    # every model today (no family in model_table.json claims audio input
    # support yet); a future model row can set it without touching this
    # dataclass again.
    audio: bool = False
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
    from halo_harness.providers.models_dev import databricks_profile_fields_from_models_dev
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
        from halo_harness.providers.cc_models import profile_fields_for_cc_model
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

    if ref.provider == "huggingface":
        # Halo 2.0.3 round 4 brief: just two tiers for `hf:` refs -- the
        # router catalog (below), else the bare dataclass default; never
        # OpenRouter's own models.json/routes.json profiles tiers (a
        # colliding id there would be a DIFFERENT model on a different
        # host) and never the vendored-fallback tier (that's keyed for
        # databricks/openrouter catalogs specifically). `ref.host` set
        # means an `hf:endpoint/<name>` ref -- a dedicated endpoint has no
        # catalog entry of its own (it was never listed by the router's
        # `GET /v1/models` at all), so this always falls to the dataclass
        # default for one. A router ref's `:suffix` (`:fastest`/`:cheapest`/
        # `:preferred`/`:<provider>`) is stripped before the catalog lookup
        # -- the catalog is keyed by bare `<org>/<model>`, never by the
        # routing suffix.
        #
        # Round 5: an `hf:local/*` ref (`ref.local`) is a THIRD tier, never
        # the router catalog above (a local server was never listed by
        # `GET /v1/models` on the router either) -- `providers.
        # huggingface_local_probe.cached_local_context_tokens` reads
        # whatever the resolved server's OWN `/v1/models` (or llama-
        # server's `/props`) reports for this exact model id, short-TTL
        # cached the same way `providers.ollama.get_catalog` is (research
        # doc section 8/10: most local servers fix context at launch time,
        # so Halo reads it back rather than requesting one); unknown (the
        # server is unreachable, or never reported a number) falls back to
        # the bare dataclass default, same as every other "nothing known
        # yet" case on this page.
        if ref.local:
            from halo_harness.providers.huggingface_local_resolve import cached_local_context_tokens
            ctx = cached_local_context_tokens(ref.host, ref.model, env=None)
            return ModelProfile(context_tokens=ctx) if ctx else ModelProfile()
        if ref.host is None:
            from halo_harness.providers.huggingface_catalog import load_hf_models_json
            bare_id = ref.model.split(":", 1)[0]
            entry = load_hf_models_json(state_dir).get(bare_id)
            if entry:
                return _profile_from_models_json_entry(entry)
        return ModelProfile()

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
        from halo_harness.providers.models_dev import (
            databricks_entries_from_full_models_dev, load_models_dev_json, load_vendored_databricks_fallback,
        )
        # (a) the REFRESHED cache `halo models --refresh` wrote to
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
        from halo_harness.providers.profiles import load_model_table
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
        from halo_harness.providers.models_dev import load_vendored_openrouter_fallback
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
        # Halo 2.0.3 round 5e: "saved versus cloud" -- set ONLY by
        # `add_savings` below, which a session calls alongside `add_usage`
        # for an ol:/hf:local/hf:mlx turn only (never for a cloud-model
        # session -- `Session.__init__` never even resolves a reference
        # price for one). `saved_price_in`/`saved_price_out`/`saved_price_
        # source` are the ONE reference price this meter's whole running
        # `saved_usd` total was computed against, resolved once at session
        # start and pinned for the session's life (switching models
        # mid-session, e.g. a hybrid-escalation auto-switch, does not
        # reprice EARLIER turns -- the figure is "what was saved so far
        # under the policy that was active", not retroactively rebased).
        self.saved_usd: float = 0.0
        self.saved_turns: int = 0
        self.saved_price_in: Optional[float] = None
        self.saved_price_out: Optional[float] = None
        self.saved_price_source: Optional[str] = None

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

    def set_savings_reference(self, *, price_in: Optional[float], price_out: Optional[float],
                               source: str) -> None:
        """Pins the ONE reference price (USD/token) `add_savings` prices
        every turn against, and `source` (a short human phrase for `/cost`'s
        own breakdown -- e.g. `"escalation target or:anthropic/claude-..."`
        or `"catalog median (30 priced models)"`) naming WHY. Called once,
        at session start, only for an ol:/hf:local/hf:mlx session
        (`model.is_local_model_ref`) -- never for a cloud-model session, so
        `saved_usd` simply never accumulates for one (pinned by tests)."""
        self.saved_price_in = price_in
        self.saved_price_out = price_out
        self.saved_price_source = source

    def add_savings(self, usage: Optional[dict]) -> Optional[float]:
        """Mirrors `add_usage`'s own `_fallback_cost` arithmetic EXACTLY
        (input*price_in + (output+reasoning)*price_out, cache tokens at
        their own rate or price_in -- pinned the same way `_fallback_cost`
        already is) but against `saved_price_in`/`saved_price_out` instead
        of this meter's own `price_in`/`price_out` -- "what this turn's
        tokens would have cost on the reference price", added to the
        running `saved_usd` total. Returns None (and changes nothing) when
        no reference price was ever set (`set_savings_reference` never
        called -- a cloud-model session) or `usage` is unusable, exactly
        the same shape `add_usage`/`_fallback_cost` already use for "price
        unknown"."""
        if self.saved_price_in is None or self.saved_price_out is None:
            return None
        saved = self._fallback_cost_at(usage, price_in=self.saved_price_in, price_out=self.saved_price_out)
        if saved is None:
            return None
        self.saved_usd += saved
        self.saved_turns += 1
        return saved

    def _fallback_cost_at(self, usage, *, price_in: float, price_out: float) -> Optional[float]:
        """The SAME formula `_fallback_cost` uses, parameterized on a
        caller-given price pair instead of `self.price_in`/`self.price_out`
        -- factored out so `add_savings` (above) can never drift from the
        ordinary cost arithmetic's own pinned behaviour."""
        if not isinstance(usage, dict):
            return None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            return None
        reasoning = usage.get("reasoning_tokens")
        reasoning = reasoning if isinstance(reasoning, int) else 0
        cache_read = usage.get("cache_read_input_tokens")
        cache_read = cache_read if isinstance(cache_read, int) else 0
        cache_write = usage.get("cache_creation_input_tokens")
        cache_write = cache_write if isinstance(cache_write, int) else 0
        return (input_tokens * price_in + (output_tokens + reasoning) * price_out
                + cache_read * price_in + cache_write * price_in)

"""tests.test_model -- model.py: ModelRef parsing (or:/dbx:/ant:/vendor-model/
routes.json aliases), ModelProfile resolution from models.json/routes.json/
defaults, CostMeter.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.model import parse_model_ref, resolve_model_profile, ModelProfile, CostMeter
from rolo_claude.providers.routing import InvalidModelError
from rolo_claude.providers.databricks import write_models_json

test, TESTS = new_registry()


@test
def test_parse_or_prefix(ctx: Ctx):
    ref = parse_model_ref("or:deepseek/deepseek-v3.2")
    ctx.check("provider openrouter", ref.provider == "openrouter")
    ctx.check("model bare", ref.model == "deepseek/deepseek-v3.2")
    ctx.check("dialect openai-chat", ref.dialect == "openai-chat")
    ctx.check("raw preserved", ref.raw == "or:deepseek/deepseek-v3.2")


@test
def test_parse_dbx_prefix_claude_is_passthrough(ctx: Ctx):
    ref = parse_model_ref("dbx:databricks-claude-sonnet-4-5")
    ctx.check("provider databricks", ref.provider == "databricks")
    ctx.check("dialect anthropic-passthrough (has 'claude')", ref.dialect == "anthropic-passthrough")


@test
def test_parse_dbx_prefix_non_claude_is_openai_chat(ctx: Ctx):
    ref = parse_model_ref("dbx:databricks-meta-llama-3-3-70b-instruct")
    ctx.check("dialect openai-chat (no 'claude' in name)", ref.dialect == "openai-chat")


@test
def test_parse_ant_prefix(ctx: Ctx):
    ref = parse_model_ref("ant:claude-opus-4")
    ctx.check("provider anthropic", ref.provider == "anthropic")
    ctx.check("dialect anthropic-passthrough", ref.dialect == "anthropic-passthrough")
    ctx.check("model bare (ant: stripped)", ref.model == "claude-opus-4")


@test
def test_parse_bare_vendor_model(ctx: Ctx):
    ref = parse_model_ref("qwen/qwen3-coder")
    ctx.check("provider openrouter (vendor/model with no prefix)", ref.provider == "openrouter")
    ctx.check("model unchanged", ref.model == "qwen/qwen3-coder")


@test
def test_parse_bare_databricks_name(ctx: Ctx):
    ref = parse_model_ref("databricks-claude-sonnet-4-5")
    ctx.check("provider databricks (bare databricks- prefix)", ref.provider == "databricks")
    ctx.check("dialect passthrough", ref.dialect == "anthropic-passthrough")


@test
def test_parse_invalid_raises(ctx: Ctx):
    try:
        parse_model_ref("totally-unrecognized-form")
        ctx.check("invalid ref must raise InvalidModelError", False)
    except InvalidModelError:
        ctx.check("InvalidModelError raised for an unrecognized ref", True)


@test
def test_alias_resolution(ctx: Ctx):
    routes = {"aliases": {"sonnet": "or:anthropic/claude-sonnet-4.5"}}
    ref = parse_model_ref("sonnet", routes)
    ctx.check("alias resolved to its target ref", ref.model == "anthropic/claude-sonnet-4.5")
    ctx.check("raw is the ALIAS name, not the resolved target", ref.raw == "sonnet")


@test
def test_alias_chain_resolution(ctx: Ctx):
    routes = {"aliases": {"a": "b", "b": "or:vendor/model-x"}}
    ref = parse_model_ref("a", routes)
    ctx.check("multi-hop alias chain resolved", ref.model == "vendor/model-x")


@test
def test_alias_self_cycle_does_not_hang(ctx: Ctx):
    routes = {"aliases": {"loop": "loop"}}
    try:
        parse_model_ref("loop", routes)
        # Falls through to "bare databricks-*/vendor-model" checks and fails
        # those too -> InvalidModelError is the expected outcome, not a hang.
        ctx.check("a self-referencing alias must not hang (raised or resolved, either is fine)", True)
    except InvalidModelError:
        ctx.check("self-cycle alias -> InvalidModelError (no hang)", True)


@test
def test_resolve_profile_from_models_json(ctx: Ctx):
    state_dir = Path(tempfile.mkdtemp(prefix="model-profile-"))
    write_models_json(state_dir, [{
        "id": "deepseek/deepseek-v3.2", "context_length": 163840, "max_output_tokens": 8192,
        "input_modalities": ["text", "image"], "supported_parameters": ["reasoning"],
        "pricing": {"prompt": "0.00000027", "completion": "0.0000011"},
    }])
    ref = parse_model_ref("or:deepseek/deepseek-v3.2")
    profile = resolve_model_profile(ref, state_dir)
    ctx.check(f"context_tokens from models.json, got {profile.context_tokens}", profile.context_tokens == 163840)
    ctx.check(f"max_output_tokens from models.json, got {profile.max_output_tokens}", profile.max_output_tokens == 8192)
    ctx.check("vision True (input_modalities has 'image')", profile.vision is True)
    ctx.check("reasoning 'openai' (supported_parameters has 'reasoning')", profile.reasoning == "openai")
    ctx.check(f"price_in parsed as float, got {profile.price_in!r}", abs(profile.price_in - 0.00000027) < 1e-12)
    ctx.check(f"price_out parsed as float, got {profile.price_out!r}", abs(profile.price_out - 0.0000011) < 1e-12)


@test
def test_h5c_extra_resolve_profile_from_models_json_parses_cache_pricing(ctx: Ctx):
    """H5c Extra (from the H8 must-do list): OpenRouter's own `pricing`
    object names the two cache fields `input_cache_read`/`input_cache_write`
    -- same per-TOKEN USD units as `prompt`/`completion` (never per-million),
    so no /1_000_000 conversion here (unlike the models.dev databricks
    fallback, which IS per-million -- see the vendored-databricks test)."""
    state_dir = Path(tempfile.mkdtemp(prefix="model-profile-cache-"))
    write_models_json(state_dir, [{
        "id": "anthropic/claude-sonnet-4.5", "context_length": 200000, "max_output_tokens": 8192,
        "pricing": {"prompt": "0.000003", "completion": "0.000015",
                    "input_cache_read": "0.0000003", "input_cache_write": "0.00000375"},
    }])
    ref = parse_model_ref("or:anthropic/claude-sonnet-4.5")
    profile = resolve_model_profile(ref, state_dir)
    ctx.check(f"price_cache_read parsed, got {profile.price_cache_read!r}",
              profile.price_cache_read is not None and abs(profile.price_cache_read - 0.0000003) < 1e-15)
    ctx.check(f"price_cache_write parsed, got {profile.price_cache_write!r}",
              profile.price_cache_write is not None and abs(profile.price_cache_write - 0.00000375) < 1e-15)


@test
def test_h5c_extra_resolve_profile_from_models_json_missing_cache_pricing_is_none(ctx: Ctx):
    """A model entry with ordinary pricing but NO cache breakdown (most
    models) must leave price_cache_read/write as None, not 0.0 or a
    KeyError -- CostMeter's own fallback then knows to approximate at
    price_in rather than treating "no data" as "free"."""
    state_dir = Path(tempfile.mkdtemp(prefix="model-profile-no-cache-"))
    write_models_json(state_dir, [{
        "id": "deepseek/deepseek-v3.2", "context_length": 163840,
        "pricing": {"prompt": "0.00000027", "completion": "0.0000011"},
    }])
    ref = parse_model_ref("or:deepseek/deepseek-v3.2")
    profile = resolve_model_profile(ref, state_dir)
    ctx.check(f"price_cache_read is None when absent, got {profile.price_cache_read!r}",
              profile.price_cache_read is None)
    ctx.check(f"price_cache_write is None when absent, got {profile.price_cache_write!r}",
              profile.price_cache_write is None)


@test
def test_resolve_profile_from_routes_json_when_no_models_json_entry(ctx: Ctx):
    state_dir = Path(tempfile.mkdtemp(prefix="model-profile-routes-"))
    ref = parse_model_ref("or:some/unknown-model")
    # Keyed by the BARE model name (ref.model), matching the proxy's own
    # resolve_profile(route.upstream_model, ...) convention -- not the
    # "or:"-prefixed raw ref.
    routes = {"profiles": {"some/unknown-model": {"context_tokens": 32000, "max_output_tokens": 4096}}}
    profile = resolve_model_profile(ref, state_dir, routes)
    ctx.check(f"context_tokens from routes.json profile, got {profile.context_tokens}", profile.context_tokens == 32000)


@test
def test_resolve_profile_default_fallback(ctx: Ctx):
    state_dir = Path(tempfile.mkdtemp(prefix="model-profile-default-"))
    ref = parse_model_ref("or:some/totally-unknown-model")
    profile = resolve_model_profile(ref, state_dir, {})
    ctx.check("falls back to the dataclass default", isinstance(profile, ModelProfile))
    ctx.check(f"default context_tokens, got {profile.context_tokens}", profile.context_tokens == 128000)


@test
def test_resolve_profile_passthrough_gets_native_reasoning_default(ctx: Ctx):
    state_dir = Path(tempfile.mkdtemp(prefix="model-profile-passthrough-"))
    ref = parse_model_ref("dbx:databricks-claude-sonnet-4-5")
    profile = resolve_model_profile(ref, state_dir, {})
    ctx.check(f"a Claude passthrough model defaults to reasoning='native', got {profile.reasoning!r}",
              profile.reasoning == "native")


@test
def test_cost_meter_openrouter_usage(ctx: Ctx):
    meter = CostMeter()
    cost1 = meter.add_usage("openrouter", {"cost": 0.001})
    cost2 = meter.add_usage("openrouter", {"cost": 0.002})
    ctx.check("first turn cost", cost1 == 0.001)
    ctx.check("second turn cost", cost2 == 0.002)
    ctx.check(f"total accumulates, got {meter.total_usd}", abs(meter.total_usd - 0.003) < 1e-9)
    ctx.check("turns counted", meter.turns == 2)
    ctx.check("has_cost_data stays True when cost is always present", meter.has_cost_data is True)


@test
def test_cost_meter_databricks_is_na(ctx: Ctx):
    meter = CostMeter()
    cost = meter.add_usage("databricks", {"input_tokens": 100, "output_tokens": 50})
    ctx.check("Databricks usage never reports cost", cost is None)
    ctx.check("has_cost_data flips False for Databricks", meter.has_cost_data is False)


@test
def test_cost_meter_missing_cost_field(ctx: Ctx):
    meter = CostMeter()
    cost = meter.add_usage("openrouter", {"input_tokens": 10})  # no "cost" key
    ctx.check("missing cost field -> None for that turn", cost is None)
    ctx.check("has_cost_data flips False once cost is unknown", meter.has_cost_data is False)


@test
def test_cost_meter_fallback_formula_when_no_usage_cost(ctx: Ctx):
    """H5 scope D: OpenCode's fallback formula -- input*price_in +
    output*price_out + reasoning*price_out -- applies whenever a per-turn
    response carries no usage.cost AND the meter was given real pricing."""
    meter = CostMeter(price_in=0.000001, price_out=0.000002)  # $1/$2 per 1M tokens
    cost = meter.add_usage("openrouter", {"input_tokens": 1000, "output_tokens": 500})
    expected = 1000 * 0.000001 + 500 * 0.000002
    ctx.check(f"fallback formula applied, got {cost}", cost is not None and abs(cost - expected) < 1e-12)
    ctx.check("has_cost_data stays True (a real, computed number)", meter.has_cost_data is True)
    ctx.check(f"total accumulates the fallback cost, got {meter.total_usd}", abs(meter.total_usd - expected) < 1e-12)


@test
def test_cost_meter_fallback_bills_reasoning_at_output_rate(ctx: Ctx):
    meter = CostMeter(price_in=0.000001, price_out=0.000002)
    cost = meter.add_usage("openrouter", {"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 200})
    expected = 100 * 0.000001 + (50 + 200) * 0.000002
    ctx.check(f"reasoning billed at the OUTPUT rate, got {cost}", cost is not None and abs(cost - expected) < 1e-12)


@test
def test_cost_meter_real_usage_cost_wins_over_fallback(ctx: Ctx):
    """A real, provider-reported usage.cost is always preferred over the
    fallback formula, even when pricing is ALSO available."""
    meter = CostMeter(price_in=0.000001, price_out=0.000002)
    cost = meter.add_usage("openrouter", {"cost": 0.0009, "input_tokens": 1000, "output_tokens": 500})
    ctx.check(f"real usage.cost used verbatim, got {cost}", cost == 0.0009)


@test
def test_cost_meter_no_pricing_no_fallback_unchanged_behavior(ctx: Ctx):
    """With NO pricing configured (the plain CostMeter() default), missing
    usage.cost still correctly reports unknown -- proves the fallback is
    opt-in and never changes pre-H5 behaviour for a caller that doesn't
    pass pricing."""
    meter = CostMeter()  # no price_in/price_out
    cost = meter.add_usage("openrouter", {"input_tokens": 1000, "output_tokens": 500})
    ctx.check("no pricing -> no fallback -> None, exactly like before", cost is None)
    ctx.check("has_cost_data flips False", meter.has_cost_data is False)


@test
def test_cost_meter_fallback_requires_both_token_counts(ctx: Ctx):
    meter = CostMeter(price_in=0.000001, price_out=0.000002)
    cost = meter.add_usage("openrouter", {"input_tokens": 1000})  # no output_tokens
    ctx.check("fallback needs BOTH input and output token counts", cost is None)


@test
def test_cost_meter_databricks_computes_fallback_when_pricing_known(ctx: Ctx):
    """1.0.1 hotfix 14 supersedes the old "Databricks is always n/a, even
    with pricing" rule: once hotfix 12 gave `resolve_model_profile` a real
    per-endpoint Databricks price (from a models.dev `databricks` catalog
    entry), a Databricks CostMeter constructed with that price must compute
    a real running cost, same fallback formula as every other provider --
    `add_usage` no longer branches on `provider` at all. See
    test_cost_meter_databricks_is_na above for the (still valid, unchanged)
    no-pricing-configured case."""
    meter = CostMeter(price_in=0.000001, price_out=0.000002)
    cost = meter.add_usage("databricks", {"input_tokens": 1000, "output_tokens": 500})
    expected = 1000 * 0.000001 + 500 * 0.000002
    ctx.check(f"Databricks now computes a fallback cost, got {cost!r}", cost == expected)
    ctx.check("has_cost_data stays True (a real figure)", meter.has_cost_data is True)


@test
def test_h5c_extra_cost_meter_fallback_prices_cache_tokens_at_their_own_rate(ctx: Ctx):
    """H5c Extra (from the H8 must-do list): cache_read_input_tokens/
    cache_creation_input_tokens are SEPARATE fields from input_tokens (see
    agent/loop.py's own _total_prompt_tokens, which sums all three for
    context-window accounting) -- before this fix the fallback formula only
    ever read input_tokens/output_tokens, so cache tokens were silently
    dropped from the bill entirely, not merely mispriced. Deliberately uses
    a cache_read rate LOWER than price_in and a cache_write rate HIGHER
    than price_in (the real-world shape for every vendor) so a test that
    accidentally fell back to price_in for either would produce a visibly
    wrong total, not one that could coincidentally match."""
    meter = CostMeter(price_in=0.000001, price_out=0.000002,
                       price_cache_read=0.0000001, price_cache_write=0.0000015)
    cost = meter.add_usage("openrouter", {
        "input_tokens": 1000, "output_tokens": 500,
        "cache_read_input_tokens": 10000, "cache_creation_input_tokens": 2000,
    })
    expected = (1000 * 0.000001 + 500 * 0.000002
                + 10000 * 0.0000001 + 2000 * 0.0000015)
    ctx.check(f"cache tokens billed at their OWN rates, got {cost}", cost is not None and abs(cost - expected) < 1e-12)
    # The wrong-but-plausible answer if cache tokens were priced at price_in
    # instead (the pre-fix "folded into input_tokens" approximation) --
    # proves this test would have caught that regression.
    wrong = (1000 * 0.000001 + 500 * 0.000002 + 10000 * 0.000001 + 2000 * 0.000001)
    ctx.check("not the price_in-for-everything approximation", cost is not None and abs(cost - wrong) > 1e-9)


@test
def test_h5c_extra_cost_meter_fallback_cache_tokens_default_to_price_in_when_unknown(ctx: Ctx):
    """A meter with ordinary price_in/price_out but NO cache-specific rates
    (most vendored/fallback rows) must still COUNT cache tokens -- at the
    ordinary input rate, the documented approximation -- never treat them
    as free just because the specific rate wasn't known."""
    meter = CostMeter(price_in=0.000001, price_out=0.000002)  # no price_cache_read/write
    cost = meter.add_usage("openrouter", {
        "input_tokens": 1000, "output_tokens": 500,
        "cache_read_input_tokens": 300, "cache_creation_input_tokens": 100,
    })
    expected = 1000 * 0.000001 + 500 * 0.000002 + 300 * 0.000001 + 100 * 0.000001
    ctx.check(f"cache tokens still counted, priced at price_in, got {cost}",
              cost is not None and abs(cost - expected) < 1e-12)
    without_cache = 1000 * 0.000001 + 500 * 0.000002
    ctx.check("cache tokens are NOT silently dropped (cost is higher than ignoring them entirely)",
              cost is not None and cost > without_cache)


# ---- H8 scope C: the vendored fallback tier ------------------------------

@test
def test_vendored_openrouter_fallback_used_when_nothing_cached(ctx: Ctx):
    """No models.json entry, no routes.json profile -- a model the package's
    OWN vendored fallback (providers/catalog/openrouter_fallback.json)
    covers still resolves real context/pricing instead of bare defaults."""
    from rolo_claude.providers.models_dev import load_vendored_openrouter_fallback
    vendored = load_vendored_openrouter_fallback()
    ctx.check("the vendored file has real entries to test against", len(vendored) > 0)
    known_id = next(iter(vendored))
    known_entry = vendored[known_id]

    state_dir = Path(tempfile.mkdtemp(prefix="model-vendored-or-"))  # empty -- no models.json cache
    ref = parse_model_ref(f"or:{known_id}")
    profile = resolve_model_profile(ref, state_dir, routes={})
    ctx.check(f"context_tokens came from the vendored entry, got {profile.context_tokens}",
              profile.context_tokens == (known_entry.get("context_length") or 128000))
    ctx.check("not just the bare dataclass default (128000/16384 with no pricing)",
              profile.price_in is not None or profile.context_tokens != 128000)


@test
def test_vendored_databricks_fallback_used_when_nothing_cached(ctx: Ctx):
    from rolo_claude.providers.models_dev import load_vendored_databricks_fallback
    vendored = load_vendored_databricks_fallback()
    ctx.check("the vendored databricks file has real entries", len(vendored) > 0)
    known_id = next(iter(vendored))
    known_entry = vendored[known_id]
    expected_context = (known_entry.get("limit") or {}).get("context")

    state_dir = Path(tempfile.mkdtemp(prefix="model-vendored-dbx-"))
    ref = parse_model_ref(f"dbx:{known_id}")
    profile = resolve_model_profile(ref, state_dir, routes={})
    if expected_context:
        ctx.check(f"context_tokens came from models.dev's databricks entry, got {profile.context_tokens}",
                  profile.context_tokens == expected_context)


@test
def test_h5c_extra_vendored_databricks_fallback_parses_cache_pricing(ctx: Ctx):
    """H5c Extra: models.dev's databricks provider entries carry a REAL
    cache_read/cache_write breakdown for Claude models (confirmed against
    providers/catalog/models_dev_databricks_fallback.json's own
    databricks-claude-sonnet-4-5 row: input 3, output 15, cache_read 0.3,
    cache_write 3.75 USD per MILLION tokens) -- resolved end to end through
    `resolve_model_profile` (no models.json cache, no routes.json profile),
    exactly the path a fresh install with no network yet takes."""
    from rolo_claude.providers.models_dev import load_vendored_databricks_fallback
    vendored = load_vendored_databricks_fallback()
    entry = vendored.get("databricks-claude-sonnet-4-5")
    ctx.check("the vendored fixture still has this row (fixture drift guard)", entry is not None)
    cost = (entry or {}).get("cost") or {}
    ctx.check(f"fixture still has a cache_read/cache_write breakdown, got {cost}",
              isinstance(cost.get("cache_read"), (int, float)) and isinstance(cost.get("cache_write"), (int, float)))

    state_dir = Path(tempfile.mkdtemp(prefix="model-vendored-dbx-cache-"))
    ref = parse_model_ref("dbx:databricks-claude-sonnet-4-5")
    profile = resolve_model_profile(ref, state_dir, routes={})
    ctx.check(f"price_in from cost.input/1e6, got {profile.price_in!r}",
              profile.price_in is not None and abs(profile.price_in - cost["input"] / 1_000_000) < 1e-15)
    ctx.check(f"price_cache_read from cost.cache_read/1e6, got {profile.price_cache_read!r}",
              profile.price_cache_read is not None
              and abs(profile.price_cache_read - cost["cache_read"] / 1_000_000) < 1e-15)
    ctx.check(f"price_cache_write from cost.cache_write/1e6, got {profile.price_cache_write!r}",
              profile.price_cache_write is not None
              and abs(profile.price_cache_write - cost["cache_write"] / 1_000_000) < 1e-15)
    ctx.check("cache_read is priced BELOW the ordinary input rate (every real vendor prices it that way)",
              profile.price_cache_read < profile.price_in)
    ctx.check("cache_write is priced ABOVE the ordinary input rate (every real vendor prices it that way)",
              profile.price_cache_write > profile.price_in)


@test
def test_models_json_cache_wins_over_vendored_fallback(ctx: Ctx):
    """A real (even if minimal) models.json entry must always beat the
    vendored package fallback -- the vendored tier is a LAST resort, never
    a way to ignore a live/cached probe result."""
    from rolo_claude.providers.models_dev import load_vendored_openrouter_fallback
    vendored = load_vendored_openrouter_fallback()
    known_id = next(iter(vendored))

    state_dir = Path(tempfile.mkdtemp(prefix="model-cache-wins-"))
    write_models_json(state_dir, [{"id": known_id, "context_length": 999, "max_output_tokens": 111}])
    ref = parse_model_ref(f"or:{known_id}")
    profile = resolve_model_profile(ref, state_dir, routes={})
    ctx.check("the cached models.json value wins, not the vendored fallback", profile.context_tokens == 999)


@test
def test_unknown_model_with_no_vendored_entry_falls_back_to_defaults(ctx: Ctx):
    state_dir = Path(tempfile.mkdtemp(prefix="model-truly-unknown-"))
    ref = parse_model_ref("or:some-vendor/totally-made-up-model-xyz")
    profile = resolve_model_profile(ref, state_dir, routes={})
    ctx.check("bare dataclass defaults, no crash", profile == ModelProfile())


# ---- H9 whole-tree review finding 22: Databricks work-default rows -------

@test
def test_h9b_f22_work_default_databricks_models_get_real_context_from_model_table(ctx: Ctx):
    """Verified bug: the vendored package fallback's 30 rows include NONE
    of the plan's own work-default models (`databricks-deepseek-v4-1-
    flash`, `-kimi-k3`, `-glm-5-3`) -- every one of them silently resolved
    to the bare 128k/16k dataclass guess, so auto-compaction triggered at
    ~89,600 tokens instead of the real ~773k (0.7 * 1,048,576) these
    models actually support. `providers/model_table.json` (a DIFFERENT
    file, for REQUEST shaping, not economics) already has a real,
    hand-verified `context_tokens` for all three -- now consulted as a
    fallback tier here too."""
    from rolo_claude.providers.profiles import load_model_table
    from rolo_claude.providers.models_dev import load_vendored_databricks_fallback
    table = load_model_table().get("databricks") or {}
    vendored = load_vendored_databricks_fallback()
    for model_id in ("databricks-deepseek-v4-1-flash", "databricks-kimi-k3", "databricks-glm-5-3"):
        ctx.check(f"fixture drift guard: model_table.json still has {model_id}", model_id in table)
        ctx.check(f"fixture drift guard: the vendored fallback still LACKS {model_id} (else this "
                  f"row would resolve via that tier instead, proving nothing)", model_id not in vendored)

        state_dir = Path(tempfile.mkdtemp(prefix="model-h9b-f22-"))
        ref = parse_model_ref(f"dbx:{model_id}")
        profile = resolve_model_profile(ref, state_dir, routes={})
        expected_context = table[model_id]["context_tokens"]
        ctx.check(f"{model_id}: context_tokens came from model_table.json, expected "
                  f"{expected_context}, got {profile.context_tokens}",
                  profile.context_tokens == expected_context)
        ctx.check(f"{model_id}: never the bare 128k dataclass guess",
                  profile.context_tokens != ModelProfile().context_tokens)


@test
def test_h9b_f22_refreshed_models_dev_cache_is_actually_read(ctx: Ctx):
    """Verified bug: `rolo-claude models --refresh` writes
    <state_dir>/models-dev.json, but `load_models_dev_json` had NO caller
    anywhere -- the refreshed data was written and then never looked at
    again by anything. Now the FIRST fallback tier tried for `databricks`,
    ahead of even the committed vendored file (a refresh the user
    explicitly ran should win over a stale committed snapshot)."""
    from rolo_claude.providers.models_dev import write_models_dev_json, load_vendored_databricks_fallback

    state_dir = Path(tempfile.mkdtemp(prefix="model-h9b-f22-refresh-"))
    fake_full_fetch = {
        "databricks": {"id": "databricks", "models": {
            "databricks-brand-new-refreshed-model": {
                "limit": {"context": 999999, "output": 12345},
                "modalities": {"input": ["text"]}, "reasoning": True,
                "cost": {"input": 1.0, "output": 2.0},
            },
        }},
        "openai": {"id": "openai", "models": {}},
    }
    write_models_dev_json(state_dir, fake_full_fetch)
    ctx.check("fixture drift guard: this model id isn't ALSO in the vendored fallback (else this "
              "proves nothing)", "databricks-brand-new-refreshed-model" not in load_vendored_databricks_fallback())

    ref = parse_model_ref("dbx:databricks-brand-new-refreshed-model")
    profile = resolve_model_profile(ref, state_dir, routes={})
    ctx.check(f"context_tokens came from the REFRESHED cache, got {profile.context_tokens}",
              profile.context_tokens == 999999)
    ctx.check(f"max_output_tokens too, got {profile.max_output_tokens}", profile.max_output_tokens == 12345)
    ctx.check(f"pricing parsed the same way the vendored tier does, got {profile.price_in!r}",
              profile.price_in is not None and abs(profile.price_in - 1.0 / 1_000_000) < 1e-15)


@test
def test_h9b_f22_refreshed_cache_wins_over_the_committed_vendored_fallback(ctx: Ctx):
    """A model present in BOTH the refreshed cache and the committed
    vendored file must resolve from the refreshed one -- it's what the
    user explicitly just pulled."""
    from rolo_claude.providers.models_dev import load_vendored_databricks_fallback, write_models_dev_json

    vendored = load_vendored_databricks_fallback()
    known_id = next(iter(vendored))

    state_dir = Path(tempfile.mkdtemp(prefix="model-h9b-f22-precedence-"))
    write_models_dev_json(state_dir, {"databricks": {"id": "databricks", "models": {
        known_id: {"limit": {"context": 424242, "output": 4242}, "modalities": {"input": ["text"]},
                   "reasoning": False, "cost": {}},
    }}})
    ref = parse_model_ref(f"dbx:{known_id}")
    profile = resolve_model_profile(ref, state_dir, routes={})
    ctx.check(f"the refreshed cache's value wins over the committed vendored file, got {profile.context_tokens}",
              profile.context_tokens == 424242)



@test
def test_parse_bare_vendor_model_needs_both_halves(ctx: Ctx):
    """1.0.1: `/effort`, `vendor/` and `a b/c` are typos, not OpenRouter
    models -- refused locally (the generic no-route error) instead of being
    accepted as `vendor/model` and failing upstream with a 400 later."""
    for bad in ("/effort", "vendor/", "a b/c"):
        refused = False
        try:
            parse_model_ref(bad)
        except InvalidModelError:
            refused = True
        ctx.check(f"{bad!r} is refused as a model ref", refused)
    ref = parse_model_ref("qwen/qwen3-coder")
    ctx.check("a real vendor/model still parses as OpenRouter",
              ref.provider == "openrouter" and ref.model == "qwen/qwen3-coder")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

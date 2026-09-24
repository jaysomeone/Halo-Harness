"""tests.test_model -- model.py: ModelRef parsing (or:/dbx:/ant:/vendor-model/
routes.json aliases), ModelProfile resolution from models.json/routes.json/
defaults, CostMeter.
"""
import json
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
        ref = parse_model_ref("loop", routes)
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


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

"""tests.test_profiles -- providers/profiles.py: profile resolution for
EVERY seeded model_table.json row (both hosts), family defaults, no
temperature on thinking endpoints, reasoning_no_disable, tool_choice
restrictions, and models.json field capture.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.providers.profiles import load_model_table, map_effort, model_family, reset_model_table_cache, resolve_profile
from rolo_claude.providers.routing import Route
from rolo_claude.providers.databricks import load_models_json, write_models_json

test, TESTS = new_registry()


def _route(provider: str, model: str, dialect: str = "openai-chat") -> Route:
    return Route(provider=provider, upstream_model=model, dialect=dialect)


@test
def test_every_seeded_model_table_row_resolves_without_raising(ctx: Ctx):
    reset_model_table_cache()
    table = load_model_table()
    total = 0
    for host, rows in table.items():
        for model_id in rows:
            total += 1
            route = _route(host, model_id)
            try:
                profile = resolve_profile(route)
            except Exception as e:
                ctx.check(f"{host}/{model_id} must resolve without raising, got {type(e).__name__}: {e}", False)
                continue
            ctx.check(f"{host}/{model_id}: max_tokens_default is a positive int or None",
                      profile.max_tokens_default is None or (isinstance(profile.max_tokens_default, int) and profile.max_tokens_default > 0))
            # H3: the host cap is 32 for Databricks, 128 for OpenRouter (plan's
            # "respecting the host cap (128 OpenRouter, 32 Databricks)") -- was
            # `None` (unlimited) for OpenRouter pre-H3; the frozen-catalog
            # selection step now relies on THIS being a real backstop.
            ctx.check(f"{host}/{model_id}: tools_max is 32 for databricks, 128 for openrouter",
                      (profile.tools_max == 32) if host == "databricks" else (profile.tools_max == 128))
    ctx.check(f"resolved at least 30 seeded rows total (found {total})", total >= 30)


@test
def test_h9_model_table_keys_are_bare_model_ids_not_prose(ctx: Ctx):
    """H9 sampling-table audit: four rows had an explanatory note baked
    straight into the JSON KEY itself -- e.g. "qwen/qwen3-max (thinking
    sibling qwen/qwen3-max-thinking)" instead of the bare id "qwen/qwen3-
    max". `resolve_profile` looks a row up by EXACT `route.upstream_model`
    match (providers/profiles.py's `model_table.get(host_key).get(route.
    upstream_model)`), so a key like that can never match any real model
    id -- the row, and all its carefully-tuned sampling/reasoning/pin
    settings, was permanently dead. This pins both the absence of the bug
    class (no key contains a space followed by '(' -- a real model id
    never does) and the specific ids that were affected now resolving to
    non-default values."""
    reset_model_table_cache()
    table = load_model_table()
    for host, rows in table.items():
        for model_id in rows:
            ctx.check(f"{host}/{model_id}: key has no parenthetical annotation baked in",
                      "(" not in model_id and ")" not in model_id)
    # The four specific ids that were previously unreachable dead rows.
    or_qwen_max = resolve_profile(_route("openrouter", "qwen/qwen3-max"))
    ctx.check("qwen/qwen3-max now resolves its real (non-family-default) row",
              or_qwen_max.top_k == 20 and or_qwen_max.edit_format == "diff")
    or_qwen_max_thinking = resolve_profile(_route("openrouter", "qwen/qwen3-max-thinking"))
    ctx.check("qwen/qwen3-max-thinking (previously nonexistent) now has its own row",
              or_qwen_max_thinking.top_k == 20)
    or_mistral_batch = resolve_profile(_route("openrouter", "mistralai/mistral-large-2512:batch"))
    ctx.check("mistralai/mistral-large-2512:batch now resolves its real row",
              or_mistral_batch.use_temperature is True and or_mistral_batch.temperature == 0.1)
    # NOTE: ProviderProfile.family comes from the separate model_family()
    # regex classifier (used only as a fallback when no exact row exists),
    # not from the row's own "family" JSON key -- that classifier has no
    # "llama" branch at all (pre-existing, unrelated to this bug: every
    # llama id, fixed or not, classifies as "generic"), so this only
    # checks fields resolve_profile DOES take from the row itself.
    dbx_maverick = resolve_profile(_route("databricks", "databricks-llama-4-maverick"))
    ctx.check("databricks-llama-4-maverick now resolves its real row",
              dbx_maverick.max_tokens_cap == 128000 and dbx_maverick.tool_choice_required_supported is True)
    dbx_maverick_alt = resolve_profile(_route("databricks", "databricks-meta-llama-4-maverick"))
    ctx.check("databricks-meta-llama-4-maverick (the function-calling-page name) also has a row",
              dbx_maverick_alt.max_tokens_cap == 128000 and dbx_maverick_alt.tool_choice_required_supported is True)
    dbx_gemma = resolve_profile(_route("databricks", "databricks-gemma-3-12b"))
    ctx.check("databricks-gemma-3-12b now resolves its real row",
              dbx_gemma.use_temperature is True and dbx_gemma.top_k == 64)


@test
def test_no_temperature_on_thinking_endpoints(ctx: Ctx):
    reset_model_table_cache()
    for host, model in (("openrouter", "deepseek/deepseek-v4.1-flash"),
                        ("databricks", "databricks-deepseek-v4-1-flash"),
                        ("openrouter", "moonshotai/kimi-k3"),
                        ("openrouter", "moonshotai/kimi-k2.7-code")):
        profile = resolve_profile(_route(host, model))
        ctx.check(f"{host}/{model}: use_temperature is False (thinking endpoint)", profile.use_temperature is False)


@test
def test_glm_5_3_reasoning_no_disable(ctx: Ctx):
    reset_model_table_cache()
    for host, model in (("openrouter", "z-ai/glm-5.3"), ("databricks", "databricks-glm-5-3"),
                        ("openrouter", "z-ai/glm-5.3-flash"), ("databricks", "databricks-glm-5-3-flash")):
        profile = resolve_profile(_route(host, model))
        ctx.check(f"{host}/{model}: reasoning_no_disable is True", profile.reasoning_no_disable is True)
        ctx.check(f"{host}/{model}: reasoning_default_effort == max", profile.reasoning_default_effort == "max")
        body_extra = map_effort("none", profile)
        got = body_extra.get("reasoning_effort") or (body_extra.get("reasoning") or {}).get("effort")
        ctx.check(f"{host}/{model}: --effort none is forced to max, got {got!r}", got == "max")


@test
def test_qwen_and_glm_families_reject_tool_choice_required(ctx: Ctx):
    reset_model_table_cache()
    for host, model in (("openrouter", "qwen/qwen3-coder"), ("openrouter", "z-ai/glm-5.3"),
                        ("openrouter", "moonshotai/kimi-k2.6"), ("databricks", "databricks-deepseek-v4-1-flash")):
        profile = resolve_profile(_route(host, model))
        ctx.check(f"{host}/{model}: tool_choice_required_supported is False", profile.tool_choice_required_supported is False)
    # K3 is the documented exception within the Kimi family.
    k3 = resolve_profile(_route("openrouter", "moonshotai/kimi-k3"))
    ctx.check("kimi-k3: tool_choice required IS supported", k3.tool_choice_required_supported is True)


@test
def test_qwen_thinking_model_never_replays_reasoning(ctx: Ctx):
    reset_model_table_cache()
    profile = resolve_profile(_route("openrouter", "qwen/qwen3-235b-a22b-thinking-2507"))
    ctx.check("qwen3-235b-thinking: reasoning_replay == empty (vendor: never replay)", profile.reasoning_replay == "empty")


@test
def test_databricks_body_allowlist_excludes_openrouter_only_fields(ctx: Ctx):
    reset_model_table_cache()
    profile = resolve_profile(_route("databricks", "databricks-kimi-k3"))
    ctx.check("provider/models/plugins/usage never in the Databricks allowlist",
              not ({"provider", "models", "plugins", "usage"} & profile.body_allowlist))
    ctx.check("reasoning_effort/stream_options ARE in the Databricks allowlist",
              {"reasoning_effort", "stream_options"} <= profile.body_allowlist)


@test
def test_openrouter_host_specific_fields_true_databricks_false(ctx: Ctx):
    reset_model_table_cache()
    or_profile = resolve_profile(_route("openrouter", "deepseek/deepseek-v4.1-flash"))
    dbx_profile = resolve_profile(_route("databricks", "databricks-deepseek-v4-1-flash"))
    ctx.check("openrouter: host_specific_fields True", or_profile.host_specific_fields is True)
    ctx.check("databricks: host_specific_fields False", dbx_profile.host_specific_fields is False)


@test
def test_anthropic_passthrough_profile(ctx: Ctx):
    profile = resolve_profile(_route("databricks", "databricks-claude-sonnet-5", dialect="anthropic-passthrough"))
    ctx.check("passthrough: thinking_format anthropic_thinking", profile.thinking_format == "anthropic_thinking")
    ctx.check("passthrough: reasoning_replay thinking", profile.reasoning_replay == "thinking")


@test
def test_model_family_classifier(ctx: Ctx):
    cases = {
        "deepseek/deepseek-v4.1-flash": "deepseek", "moonshotai/kimi-k3": "kimi",
        "z-ai/glm-5.3": "glm", "qwen/qwen3-coder": "qwen", "google/gemini-3.5-flash": "gemini",
        "anthropic/claude-sonnet-5": "claude", "x-ai/grok-4": "grok", "openai/gpt-5": "gpt",
        "some/totally-unknown-model": "generic",
    }
    for model_id, expected in cases.items():
        ctx.check(f"model_family({model_id!r}) == {expected!r}, got {model_family(model_id)!r}", model_family(model_id) == expected)


@test
def test_models_json_extended_fields_captured(ctx: Ctx):
    """scope I: models.json stores supported_parameters, input_modalities,
    pricing, in addition to context_length/max_output_tokens."""
    state_dir = Path(tempfile.mkdtemp(prefix="profiles-modelsjson-"))
    write_models_json(state_dir, [{
        "id": "deepseek/deepseek-v4.1-flash", "context_length": 1048576, "max_output_tokens": 65536,
        "input_modalities": ["text", "image"], "supported_parameters": ["reasoning", "tools"],
        "pricing": {"prompt": "0.0000003", "completion": "0.0000012"},
        "top_provider": {"context_length": 1048576, "max_completion_tokens": 393216},
    }])
    loaded = load_models_json(state_dir)
    entry = loaded["deepseek/deepseek-v4.1-flash"]
    ctx.check("context_length captured", entry.get("context_length") == 1048576)
    ctx.check("input_modalities captured", entry.get("input_modalities") == ["text", "image"])
    ctx.check("supported_parameters captured", entry.get("supported_parameters") == ["reasoning", "tools"])
    ctx.check("pricing captured", entry.get("pricing", {}).get("prompt") == "0.0000003")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

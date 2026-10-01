"""tests.test_hotfix_101_fix_chat_effort_sets -- 1.0.1 fixpass finding 13:
chat-dialect routes (Databricks openai-chat, OpenRouter) no longer accept
"max" (an Anthropic-only `output_config.effort` level) by default -- the
`/effort` card (tui/slash.py) reads `profile.effort_values_supported`
directly, so narrowing it here is the whole UI-visible fix too. A tabled
row whose OWN `reasoning_default_effort` genuinely needs a value outside
the narrowed default (GLM-5.3's real "max") still gets it. The effort-
retry classifier now also recognises OpenRouter's dotted "reasoning.effort"
wording and a generic OpenAI-style "Invalid value: 'max'" 400.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _route(host, model):
    from rolo_claude.providers.routing import Route
    return Route(provider=host, upstream_model=model, dialect="openai-chat")


@test
def test_databricks_chat_route_no_longer_accepts_max(ctx: Ctx):
    from rolo_claude.providers.profiles import reset_model_table_cache, resolve_profile
    reset_model_table_cache()
    # gpt-oss-120b: a real tabled row with an ordinary (not "max") default.
    profile = resolve_profile(_route("databricks", "databricks-gpt-oss-120b"))
    ctx.check(f"no 'max' in the accepted set, got {profile.effort_values_supported}",
              "max" not in profile.effort_values_supported)
    ctx.check(f"xhigh is still offered (OpenAI-dialect-style, genuinely meaningful here), got "
              f"{profile.effort_values_supported}", "xhigh" in profile.effort_values_supported)


@test
def test_openrouter_chat_route_no_longer_accepts_max(ctx: Ctx):
    from rolo_claude.providers.profiles import reset_model_table_cache, resolve_profile
    reset_model_table_cache()
    profile = resolve_profile(_route("openrouter", "deepseek/deepseek-v4.1-flash"))
    ctx.check(f"no 'max' in the accepted set, got {profile.effort_values_supported}",
              "max" not in profile.effort_values_supported)


@test
def test_clamp_effort_downgrades_max_on_a_plain_chat_route(ctx: Ctx):
    """The end-to-end consequence: /effort max (or a stale settings value)
    on an ordinary chat route no longer reaches the wire as 'max' -- it
    downgrades instead of 400ing every turn until the user changes it.

    1.0.1 part 2 reviewer minor 2 update: gpt-oss-120b's row (checked
    just above) lists `xhigh` as its own strongest real level, so `max`
    now downgrades specifically TO `xhigh` (that route's equivalent
    ceiling) rather than falling all the way to the bland "medium"
    default -- the plain-default fallback for a route with neither `max`
    nor `xhigh` is covered separately in
    tests/test_hotfix_101_effort.py::
    test_clamp_effort_max_falls_back_to_default_on_a_route_with_neither_max_nor_xhigh."""
    from rolo_claude.providers.profiles import clamp_effort, reset_model_table_cache, resolve_profile
    reset_model_table_cache()
    profile = resolve_profile(_route("databricks", "databricks-gpt-oss-120b"))
    clamped = clamp_effort("max", profile)
    ctx.check(f"'max' is downgraded, never sent as-is, got {clamped!r}", clamped != "max")
    ctx.check(f"downgrades to this route's own xhigh ceiling, got {clamped!r}", clamped == "xhigh")


@test
def test_data_driven_row_default_outside_the_base_set_is_still_honoured(ctx: Ctx):
    """GLM-5.3's row documents "max" as its own REAL default (reasoning_
    no_disable forces a disabling --effort UP to it) -- the narrower
    default must not make clamp_effort then reject that same value as
    unsupported and silently fall back to "medium" instead."""
    from rolo_claude.providers.profiles import map_effort, reset_model_table_cache, resolve_profile
    reset_model_table_cache()
    for host, model in (("openrouter", "z-ai/glm-5.3"), ("databricks", "databricks-glm-5-3")):
        profile = resolve_profile(_route(host, model))
        ctx.check(f"{host}/{model}: 'max' is in the accepted set (the row's own real default), got "
                  f"{profile.effort_values_supported}", "max" in profile.effort_values_supported)
        body = map_effort("none", profile)  # reasoning_no_disable forces this UP to "max"
        got = body.get("reasoning_effort") or (body.get("reasoning") or {}).get("effort")
        ctx.check(f"{host}/{model}: still forced to max, got {got!r}", got == "max")


# ---------------------------------------------------------------------------
# is_effort_rejected_message: two more wordings.
# ---------------------------------------------------------------------------

@test
def test_recognises_openrouter_dotted_reasoning_effort_path(ctx: Ctx):
    from rolo_claude.providers.errors import is_effort_rejected_message
    ctx.check("matches OpenRouter's nested reasoning.effort wording",
              is_effort_rejected_message("Invalid value for 'reasoning.effort': 'max' is not supported."))


@test
def test_recognises_generic_openai_invalid_value_wording(ctx: Ctx):
    from rolo_claude.providers.errors import is_effort_rejected_message
    ctx.check("matches a plain OpenAI-style enum-validation 400 for 'max'",
              is_effort_rejected_message("Invalid value: 'max'. Supported values are: 'low', 'medium', and 'high'."))
    ctx.check("matches the same wording for 'xhigh'",
              is_effort_rejected_message("Invalid value: 'xhigh'. Supported values are: 'low', 'medium', and 'high'."))
    ctx.check("an unrelated 'Invalid value' 400 (no effort level named) does NOT match -- never a blanket retry",
              not is_effort_rejected_message("Invalid value: 'true'. 'stream' must be a boolean."))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

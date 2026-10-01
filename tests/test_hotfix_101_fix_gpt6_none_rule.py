"""tests.test_hotfix_101_fix_gpt6_none_rule -- 1.0.1 fixpass finding 12: the
gpt-6 "reasoning_effort:none with tools" rule is Databricks-only (it used to
sit in the SHARED hook_fields dict, splatted into the OpenRouter branch too,
silently disabling reasoning and making `/effort` a no-op for any
OpenRouter "gpt-6"-named model); it is driven by explicit model_table.json
rows for both real models.dev name shapes (`databricks-gpt-6-*` and
`databricks-gpt-5-6-*`), since the OLD bare "gpt-6" substring check is not
even a substring of the real Databricks catalog naming
`databricks-gpt-5-6-{sol,luna,terra}`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


@test
def test_openrouter_gpt6_named_model_does_not_get_the_rule(ctx: Ctx):
    """The core bug: an OpenRouter model whose id happens to contain
    "gpt-6" must NOT get reasoning silently disabled on every tool turn."""
    from halo_harness.providers.profiles import map_effort, reset_model_table_cache, resolve_profile
    from halo_harness.providers.routing import Route
    reset_model_table_cache()
    route = Route(provider="openrouter", upstream_model="openai/gpt-6", dialect="openai-chat")
    profile = resolve_profile(route)
    ctx.check(f"OpenRouter profile carries NO override, got {profile.reasoning_effort_with_tools!r}",
              profile.reasoning_effort_with_tools is None)
    body = map_effort("high", profile, has_tools=True)
    ctx.check(f"/effort is NOT a no-op here -- the requested value reaches the wire, got {body}",
              body == {"reasoning": {"effort": "high"}})


@test
def test_real_databricks_catalog_naming_gpt_5_6_sol_gets_the_rule(ctx: Ctx):
    """finding 12's own naming bug: models.dev's real Databricks catalog
    names these `databricks-gpt-5-6-{sol,luna,terra}` -- the bare "gpt-6"
    substring the old code checked for is not even a substring of that."""
    from halo_harness.providers.profiles import map_effort, reset_model_table_cache, resolve_profile
    from halo_harness.providers.routing import Route
    reset_model_table_cache()
    for suffix in ("sol", "luna", "terra"):
        model_id = f"databricks-gpt-5-6-{suffix}"
        route = Route(provider="databricks", upstream_model=model_id, dialect="openai-chat")
        profile = resolve_profile(route)
        ctx.check(f"{model_id}: profile carries the override, got {profile.reasoning_effort_with_tools!r}",
                  profile.reasoning_effort_with_tools == "none")
        body = map_effort("high", profile, has_tools=True)
        ctx.check(f"{model_id}: forced to none despite --effort high, got {body}",
                  body == {"reasoning_effort": "none"})
        body_no_tools = map_effort("high", profile, has_tools=False)
        ctx.check(f"{model_id}: unaffected without tools, got {body_no_tools}",
                  body_no_tools == {"reasoning_effort": "high"})


@test
def test_databricks_gpt_6_naming_still_gets_the_rule_too(ctx: Ctx):
    """Both real name shapes are tabled -- `databricks-gpt-6-*` (the
    originally-verified live wording) must keep working exactly as before."""
    from halo_harness.providers.profiles import reset_model_table_cache, resolve_profile
    from halo_harness.providers.routing import Route
    reset_model_table_cache()
    for suffix in ("sol", "luna", "terra"):
        model_id = f"databricks-gpt-6-{suffix}"
        route = Route(provider="databricks", upstream_model=model_id, dialect="openai-chat")
        profile = resolve_profile(route)
        ctx.check(f"{model_id}: profile carries the override, got {profile.reasoning_effort_with_tools!r}",
                  profile.reasoning_effort_with_tools == "none")


@test
def test_rule_is_driven_by_an_explicit_model_table_row(ctx: Ctx):
    """The worker's report for item 22 claimed model_table.json was changed
    but it was not in that change set -- this pins that the row genuinely
    exists now (not a substring guess): an explicit `null` override in the
    table must be honoured over the fallback, proving `row.get(...)` -- not
    a hardcoded substring check -- is what actually decides this."""
    from halo_harness.providers.profiles import load_model_table, reset_model_table_cache, resolve_profile
    from halo_harness.providers.routing import Route
    reset_model_table_cache()
    table = load_model_table()
    for name in ("databricks-gpt-6-sol", "databricks-gpt-6-luna", "databricks-gpt-6-terra",
                 "databricks-gpt-5-6-sol", "databricks-gpt-5-6-luna", "databricks-gpt-5-6-terra"):
        row = table.get("databricks", {}).get(name)
        ctx.check(f"{name}: an explicit model_table.json row exists", isinstance(row, dict))
        ctx.check(f"{name}: the row itself sets reasoning_effort_with_tools='none', got {row}",
                  row.get("reasoning_effort_with_tools") == "none")

    # An explicit `null` in the table opts a model back OUT, overriding
    # even the last-resort substring net -- proves row.get(..., fallback)
    # precedence, not a hardcoded "always none for gpt-6" shortcut.
    patched = {"databricks": {**table.get("databricks", {}),
                              "databricks-gpt-6-opted-out": {"reasoning_effort_with_tools": None}}}
    route = Route(provider="databricks", upstream_model="databricks-gpt-6-opted-out", dialect="openai-chat")
    profile = resolve_profile(route, model_table=patched)
    ctx.check(f"an explicit null row opts back out, got {profile.reasoning_effort_with_tools!r}",
              profile.reasoning_effort_with_tools is None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

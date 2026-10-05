"""tests.test_compact -- agent/compact.py: trigger math (three profiles),
knobs precedence, tail-retention formula, atomic-unit-aware tail selection,
8-heading validation + corrective retry, prior-summary detection."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.agent.compact import (
    MIN_VIABLE_RESERVE_TOKENS, SUMMARY_HEADINGS, CompactionKnobs, build_summary_instruction,
    compaction_trigger_tokens, dsh_trigger_tokens, find_prior_summary, opencode_usable, resolve_knobs,
    select_verbatim_tail, should_compact, tail_retention_tokens, validate_summary, wrap_compacted_summary,
)

test, TESTS = new_registry()

# Three representative profiles (small/Databricks-128k-class,
# mid/OpenRouter-200k-class, large/1M-class).
_PROFILES = {
    "small_128k": (128_000, 16_384),
    "mid_200k": (200_000, 8_192),
    "large_1m": (1_000_000, 32_000),
}


@test
def test_trigger_math_three_profiles_positive_and_below_context(ctx: Ctx):
    for name, (context, max_out) in _PROFILES.items():
        trigger = compaction_trigger_tokens(context, max_out)
        ctx.check(f"{name}: trigger > 0, got {trigger}", trigger > 0)
        ctx.check(f"{name}: trigger < context window, got {trigger} vs {context}", trigger < context)
        ctx.check(f"{name}: trigger never exceeds opencode's usable floor",
                  trigger <= opencode_usable(context, max_out))


@test
def test_trigger_is_deterministic_pure_function(ctx: Ctx):
    a = compaction_trigger_tokens(200_000, 8192)
    b = compaction_trigger_tokens(200_000, 8192)
    ctx.check("same inputs -> same trigger", a == b)


@test
def test_tiny_context_window_clamps_to_zero_not_negative(ctx: Ctx):
    # CLAUDE_CODE_AUTO_COMPACT_WINDOW=20000 against the 65,536 headroom
    # constant drives the raw formula negative -- must clamp to 0 (compact
    # on the very next usage update), never go negative.
    t = dsh_trigger_tokens(20_000, 8192)
    ctx.check(f"tiny window clamps to 0, got {t}", t == 0)


@test
def test_opencode_usable_formula(ctx: Ctx):
    # usable = (context - capped_max_output) - min(20_000, capped_max_output),
    # capped_max_output = min(32_000, max_output) -- finding 1 (h4-h5-h3c
    # review): the model's advertised max_output is capped at 32,000
    # BEFORE it is used anywhere in this formula, same as OpenCode's own
    # `max_output_tokens(model, cap=32_000)`.
    u = opencode_usable(200_000, 8_192)
    ctx.check(f"usable matches the formula exactly, got {u}", u == (200_000 - 8_192) - min(20_000, 8_192))
    u2 = opencode_usable(100_000, 50_000)  # 50_000 > the 32_000 output cap
    ctx.check(f"max_output is capped at 32,000 before reserved/base both use it, got {u2}",
              u2 == (100_000 - 32_000) - min(20_000, 32_000))


@test
def test_h5b_f01_opencode_usable_caps_advertised_output_at_32k(ctx: Ctx):
    """finding 1's own verified repro, reproduced directly: Kimi K2.6's
    real models.json shape (262,144 context / 235,929 advertised
    max_output) used to collapse `usable` to a low single-digit-thousands
    value (and the dsh trigger to 0, clamped) because the whole advertised
    max_output was subtracted un-capped. Capped at 32,000, `usable` must
    be a large, sane fraction of the context window."""
    kimi_context, kimi_max_output = 262_144, 235_929
    u = opencode_usable(kimi_context, kimi_max_output)
    expected = (kimi_context - 32_000) - min(20_000, 32_000)
    ctx.check(f"usable uses the capped output, got {u} (expected {expected})", u == expected)
    ctx.check(f"usable is now a large share of the window, got {u} of {kimi_context}", u > kimi_context * 0.5)


@test
def test_pct_override_can_only_lower(ctx: Ctx):
    baseline = compaction_trigger_tokens(200_000, 8192)
    lowered = compaction_trigger_tokens(200_000, 8192, pct_override=40)
    raised = compaction_trigger_tokens(200_000, 8192, pct_override=95)  # must NOT raise above 80%
    ctx.check("a lower pct_override lowers the trigger", lowered < baseline)
    ctx.check("a HIGHER pct_override never raises the trigger above the 80% default", raised == baseline)


@test
def test_disable_compact_env_wins(ctx: Ctx):
    knobs = resolve_knobs(None, {"DISABLE_COMPACT": "1"})
    ctx.check("DISABLE_COMPACT=1 disables", knobs.disabled is True)
    should, trigger = should_compact(999_999_999, 200_000, 8192, knobs)
    ctx.check("should_compact is False when disabled regardless of usage", should is False)
    ctx.check("trigger reported as 0 when disabled", trigger == 0)


@test
def test_h5b_f01_real_models_json_zero_trigger_rows_now_get_a_sane_trigger(ctx: Ctx):
    """finding 1: 29 of 105 real DeepSeek/Kimi/GLM/Qwen/MiniMax rows in
    the owner's own ~/.halo/models.json got a trigger of EXACTLY 0 (21
    more under 40k) because the gate subtracted the model's raw advertised
    max_output_tokens -- often close to the whole context window -- with
    no 32k cap. Fixture = the exact (context_length, max_output_tokens)
    pairs copied from the real file for the rows the finding names by
    name. Every one must now land at a large, usable fraction of its own
    context window, never anywhere near 0."""
    # name -> (context_length, max_output_tokens), copied verbatim from
    # ~/.halo/models.json on 2026-09-24.
    real_rows = {
        "moonshotai/kimi-k2.5": (262_144, 235_929),
        "moonshotai/kimi-k2.6": (262_144, 235_929),
        "moonshotai/kimi-k2.7-code": (262_144, 235_929),
        "qwen/qwen3-coder-next": (262_144, 235_929),
        "qwen/qwen3.5-397b-a17b": (262_144, 235_929),
        "minimax/minimax-m2": (204_800, 176_947),
        "z-ai/glm-5": (204_800, 128_000),
        "deepseek/deepseek-v3.2": (163_840, 65_536),
    }
    for name, (context, max_out) in real_rows.items():
        # The OLD (buggy) formula, for documentation/contrast only -- NOT
        # asserted as today's behaviour, just proof this fixture really is
        # one of the verified zero/near-zero rows.
        old_dsh = max(0, int(0.8 * (context - max_out - 65_536)))
        old_usable = max(0, (context - max_out) - min(20_000, max_out))
        old_trigger = min(old_dsh, old_usable)

        trigger = compaction_trigger_tokens(context, max_out)
        ctx.check(f"{name}: trigger is a large share of context, got {trigger} of {context} "
                  f"(old buggy formula gave {old_trigger})", trigger >= int(context * 0.5))
        ctx.check(f"{name}: trigger stays below the context window, got {trigger}", trigger < context)


@test
def test_h5b_f03_small_window_profile_from_the_finding_itself(ctx: Ctx):
    """H5b finding 3 (major): `min(usable, floored)` let `usable` override
    the ~70% floor whenever `context - min(out,32k) - min(20k,out) <= 0` --
    a SMALL window whose advertised max_output is itself a big fraction of
    it. The finding's own worked example, quoted directly in its text:
    "With a 32,768/29,491 profile ... each skipped step emits
    compaction phase='failed'" -- the old formula gave EXACTLY 0 for this
    pair (asserted below as documentation, not as today's behaviour)."""
    context, max_out = 32_768, 29_491
    old_dsh = max(0, int(0.8 * (context - min(32_000, max_out) - 65_536)))
    old_usable = max(0, (context - min(32_000, max_out)) - min(20_000, min(32_000, max_out)))
    old_trigger = min(old_dsh, old_usable)
    ctx.check(f"sanity: this really is one of the verified zero-trigger shapes, old={old_trigger}",
              old_trigger == 0)

    trigger = compaction_trigger_tokens(context, max_out)
    ctx.check(f"trigger is no longer 0, got {trigger}", trigger > 0)
    ctx.check(f"trigger is a large share of the window (the ~70% floor), got {trigger} of {context}",
              trigger >= int(context * 0.65))
    ctx.check(f"trigger stays below the context window, got {trigger}", trigger < context)


@test
def test_h5b_f03_floor_and_reserve_both_hold(ctx: Ctx):
    """"floor + reserve must both hold": the floor may raise the trigger
    for a small window, but never so far that less than
    MIN_VIABLE_RESERVE_TOKENS of headroom is left in the window -- the
    floor must never crowd out room for a real reply entirely."""
    for context, max_out in [(32_768, 29_491), (32_768, 32_768), (65_536, 65_536), (16_384, 16_384)]:
        trigger = compaction_trigger_tokens(context, max_out)
        ctx.check(f"({context},{max_out}): trigger + min reserve <= context, got trigger={trigger}",
                  trigger + MIN_VIABLE_RESERVE_TOKENS <= context)
        ctx.check(f"({context},{max_out}): trigger is still meaningfully positive, got {trigger}",
                  trigger > 0)


@test
def test_h5b_f03_small_window_open_weight_shapes_no_longer_zero(ctx: Ctx):
    """The class of real row this finding names: 32k-64k-class context with
    an advertised max_output close to or equal to the whole context, which
    is what OpenRouter reports for a model whose provider listing gives no
    SEPARATE, smaller completion cap."""
    # (65_536, 65_536) reproduces the finding's OWN separately-quoted "nine
    # more rows sit under 50%, for example 65,536-window rows at 13,536"
    # number exactly (old_usable == 13_536 below) -- a DIFFERENT, less
    # severe old-formula symptom than the ~0 rows, still fixed the same way.
    shapes = [(32_768, 32_768), (32_768, 30_000), (65_536, 65_536), (16_384, 16_384)]
    old_usables = {}
    for context, max_out in shapes:
        old_usables[(context, max_out)] = max(0, (context - min(32_000, max_out)) - min(20_000, min(32_000, max_out)))
        trigger = compaction_trigger_tokens(context, max_out)
        ctx.check(f"({context},{max_out}): fixed trigger is sane, got {trigger}", trigger >= int(context * 0.5))
    ctx.check(f"the 65,536-window row's old `usable` was 13,536 (the finding's own quoted number), "
              f"got {old_usables[(65_536, 65_536)]}", old_usables[(65_536, 65_536)] == 13_536)


@test
def test_h5c_f24_the_exact_zero_trigger_rows_the_finding_names_are_fixed(ctx: Ctx):
    """H5c finding 24: `test_h5b_f01_real_models_json_zero_trigger_rows...`
    only covers finding 1's 8 LARGE-window rows (Kimi/Qwen3.5/MiniMax/
    GLM-5/DeepSeek-v3.2) -- never the actual ZERO-trigger rows finding 3
    itself names. These 5 exact (context_length, max_output_tokens) pairs
    are copied verbatim from the owner's real ~/.halo/models.json on
    2026-09-24 for the finding's own named ids, and verified below to
    genuinely give the OLD formula a trigger of exactly 0 (matching
    finding 3's "21 of 458 real models.json rows still have a trigger of
    0... Five of them are in the target families") before asserting the
    FIXED trigger is sane for every one of them."""
    real_zero_trigger_rows = {
        "z-ai/glm-5.2:free": (32_768, 29_491),
        "deepseek/deepseek-r1-distill-llama-70b": (8_192, 7_372),
        "qwen/qwen-2.5-coder-32b-instruct": (32_768, 29_491),
        "qwen/qwen-2.5-7b-instruct": (32_768, 29_491),
        "qwen/qwen-2.5-72b-instruct": (32_768, 16_384),
    }
    for name, (context, max_out) in real_zero_trigger_rows.items():
        old_dsh = max(0, int(0.8 * (context - min(32_000, max_out) - 65_536)))
        old_usable = max(0, (context - min(32_000, max_out)) - min(20_000, min(32_000, max_out)))
        old_trigger = min(old_dsh, old_usable)
        ctx.check(f"{name}: sanity -- this really is one of the verified zero-trigger rows, got old={old_trigger}",
                  old_trigger == 0)

        trigger = compaction_trigger_tokens(context, max_out)
        ctx.check(f"{name}: trigger is no longer 0, got {trigger}", trigger > 0)
        # The ~70% floor is capped by the min-viable-reserve ceiling for a
        # TINY context (deepseek-r1-distill-llama-70b's 8,192-token window):
        # the real invariant that holds for every row is "at least the
        # floor, unless the reserve ceiling caps it lower first".
        expected_floor = min(int(context * 0.65), context - MIN_VIABLE_RESERVE_TOKENS)
        ctx.check(f"{name}: trigger meets the floor (or the reserve ceiling that caps it), got {trigger} of {context}",
                  trigger >= expected_floor)
        ctx.check(f"{name}: trigger stays below the context window, got {trigger}", trigger < context)


@test
def test_disable_compact_falsy_values_do_not_disable(ctx: Ctx):
    for v in ("0", "false", "", "no"):
        knobs = resolve_knobs(None, {"DISABLE_COMPACT": v})
        ctx.check(f"DISABLE_COMPACT={v!r} does not disable", knobs.disabled is False)


@test
def test_window_override_env_wins_over_settings(ctx: Ctx):
    class FakeSettings:
        auto_compact_window = 500_000
        auto_compact_enabled = True
        compaction_model = None
        raw = {}

    knobs = resolve_knobs(FakeSettings(), {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "20000"})
    ctx.check("env wins over settings.autoCompactWindow", knobs.window_override == 20_000)

    knobs2 = resolve_knobs(FakeSettings(), {})
    ctx.check("falls back to settings.autoCompactWindow when env is absent", knobs2.window_override == 500_000)


@test
def test_window_override_cli_flag_wins_over_env_and_settings(ctx: Ctx):
    """Release review finding 34: a settings.json env block setting
    `CLAUDE_CODE_AUTO_COMPACT_WINDOW` used to silently outrank the
    explicit `--autocompact` flag (cli.py wrote the flag's value into the
    LOWEST/shell layer of `effective_env`, which a settings env block
    then overrides). `cli_autocompact` now wins over everything else."""
    class FakeSettings:
        auto_compact_window = 500_000
        auto_compact_enabled = True
        compaction_model = None
        raw = {}

    knobs = resolve_knobs(FakeSettings(), {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "20000"},
                           cli_autocompact="9000")
    ctx.check(f"the CLI flag wins over BOTH env and settings, got {knobs.window_override}",
              knobs.window_override == 9_000)

    knobs_k_suffix = resolve_knobs(FakeSettings(), {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "20000"},
                                    cli_autocompact="9k")
    ctx.check(f"a k-suffixed CLI value is parsed as thousands, got {knobs_k_suffix.window_override}",
              knobs_k_suffix.window_override == 9_000)

    knobs_auto = resolve_knobs(FakeSettings(), {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "20000"},
                                cli_autocompact="auto")
    ctx.check(f"--autocompact auto defers to the next-lower source (env), got {knobs_auto.window_override}",
              knobs_auto.window_override == 20_000)

    knobs_none = resolve_knobs(FakeSettings(), {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "20000"})
    ctx.check(f"no CLI flag at all still falls back to env, got {knobs_none.window_override}",
              knobs_none.window_override == 20_000)


@test
def test_window_override_auto_string_ignored(ctx: Ctx):
    class FakeSettings:
        auto_compact_window = "auto"
        auto_compact_enabled = True
        compaction_model = None
        raw = {}

    knobs = resolve_knobs(FakeSettings(), {})
    ctx.check("settings.autoCompactWindow == 'auto' means no override", knobs.window_override is None)


@test
def test_auto_compact_enabled_false_disables(ctx: Ctx):
    class FakeSettings:
        auto_compact_window = None
        auto_compact_enabled = False
        compaction_model = None
        raw = {}

    knobs = resolve_knobs(FakeSettings(), {})
    should, _ = should_compact(999_999_999, 200_000, 8192, knobs)
    ctx.check("autoCompactEnabled=false disables auto-compaction", should is False)


@test
def test_compaction_model_from_settings_raw(ctx: Ctx):
    """H5c finding 18: settings.raw is only the FALLBACK -- isolated with
    an empty `BRIDGE_TEST_HOME` (never the real machine's own
    `~/.halo/config.json`) so this stays deterministic regardless
    of what that file happens to contain wherever the suite runs."""
    import os
    import tempfile

    class FakeSettings:
        auto_compact_window = None
        auto_compact_enabled = True
        compaction_model = None
        raw = {"compactionModel": "or:some/small-model"}

    old = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = tempfile.mkdtemp(prefix="halo-compactcfg-empty-")
    try:
        knobs = resolve_knobs(FakeSettings(), {})
        ctx.check("compactionModel read from settings.raw (no config.json override present)",
                  knobs.compaction_model == "or:some/small-model")
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old


@test
def test_h5c_f18_compaction_model_from_halo_harness_config_json_wins_over_settings(ctx: Ctx):
    """H5c finding 18: `~/.halo/config.json`'s own `compactionModel`
    key (brief D: "can point at the small one") wins over Claude Code's
    settings chain -- the OLD code read settings ONLY, silently ignoring
    it. Driven through the REAL config path (`theme.set_config_value` ->
    `~/.halo/config.json`, redirected to an isolated
    `BRIDGE_TEST_HOME`), never an injected knob."""
    import os
    import tempfile

    from halo_harness import theme as theme_mod

    class FakeSettings:
        auto_compact_window = None
        auto_compact_enabled = True
        compaction_model = None
        raw = {"compactionModel": "or:from-settings/should-lose"}

    old = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = tempfile.mkdtemp(prefix="halo-compactcfg-")
    try:
        theme_mod.set_config_value("compactionModel", "or:from-config-json/should-win")
        knobs = resolve_knobs(FakeSettings(), {})
        ctx.check(f"config.json's compactionModel wins over settings.raw, got {knobs.compaction_model!r}",
                  knobs.compaction_model == "or:from-config-json/should-win")
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old


@test
def test_h5c_f18_compaction_model_falls_back_to_settings_when_config_json_has_no_key(ctx: Ctx):
    """H5c finding 18: an empty/missing `~/.halo/config.json` (or
    one with unrelated keys) must still fall back to settings -- config.json
    winning is a PRIORITY rule, not a requirement that it be set at all."""
    import os
    import tempfile

    from halo_harness import theme as theme_mod

    class FakeSettings:
        auto_compact_window = None
        auto_compact_enabled = True
        compaction_model = None
        raw = {"compactionModel": "or:some/small-model"}

    old = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = tempfile.mkdtemp(prefix="halo-compactcfg-unrelated-")
    try:
        theme_mod.set_config_value("theme", "claude-dark")  # a real key, just not compactionModel
        knobs = resolve_knobs(FakeSettings(), {})
        ctx.check(f"falls back to settings.raw, got {knobs.compaction_model!r}",
                  knobs.compaction_model == "or:some/small-model")
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old


@test
def test_should_compact_true_once_above_trigger(ctx: Ctx):
    knobs = CompactionKnobs()
    trigger = compaction_trigger_tokens(200_000, 8192)
    below, _ = should_compact(trigger - 1, 200_000, 8192, knobs)
    above, _ = should_compact(trigger, 200_000, 8192, knobs)
    ctx.check("below the trigger -> False", below is False)
    ctx.check("at/above the trigger -> True", above is True)


@test
def test_tail_retention_formula_bounds(ctx: Ctx):
    ctx.check("floors at 2,000", tail_retention_tokens(1000) == 2000)
    ctx.check("caps at 15,000", tail_retention_tokens(1_000_000) == 15_000)
    ctx.check("25% of usable in the middle", tail_retention_tokens(40_000) == 10_000)


def _assistant_tool_use(tid: str) -> dict:
    return {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "Read", "input": {}}]}


def _tool_result(tid: str, text: str = "x") -> dict:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}


@test
def test_select_verbatim_tail_never_splits_tool_use_pair(ctx: Ctx):
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "q1"}]},
        _assistant_tool_use("t1"), _tool_result("t1"),
        {"role": "user", "content": [{"type": "text", "text": "q2"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "a2"}]},
    ]
    # A budget that would land exactly between the tool_use and its result if
    # split naively by message count.
    tail = select_verbatim_tail(messages, tail_tokens=1)  # tiny budget -> at least the last unit
    ctx.check("keeps at least one full unit even under budget", len(tail) >= 1)
    # Any tail that includes a tool_use message must also include its result.
    ids_with_tool_use = {b["id"] for m in tail if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"}
    ids_with_result = {b["tool_use_id"] for m in tail if m["role"] == "user" for b in m["content"] if b.get("type") == "tool_result"}
    ctx.check(f"every kept tool_use has its tool_result in the tail too, ids_with_tool_use={ids_with_tool_use}",
              ids_with_tool_use <= ids_with_result)


@test
def test_select_verbatim_tail_keeps_everything_when_budget_is_large(ctx: Ctx):
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    tail = select_verbatim_tail(messages, tail_tokens=1_000_000)
    ctx.check("everything fits under a huge budget", tail == messages)


@test
def test_select_verbatim_tail_empty_input(ctx: Ctx):
    ctx.check("empty messages -> empty tail", select_verbatim_tail([], 1000) == [])


@test
def test_validate_summary_all_headings_present(ctx: Ctx):
    text = "\n".join(f"## {h}\ncontent" for h in SUMMARY_HEADINGS)
    ok, missing = validate_summary(text, stop_reason="end_turn")
    ctx.check(f"all 8 headings present -> ok, missing={missing}", ok is True)
    ctx.check("no missing headings", missing == [])


@test
def test_validate_summary_missing_heading(ctx: Ctx):
    text = "\n".join(f"## {h}\ncontent" for h in SUMMARY_HEADINGS[:-1])  # drop "Critical Context"
    ok, missing = validate_summary(text, stop_reason="end_turn")
    ctx.check("missing a heading -> not ok", ok is False)
    ctx.check("the missing heading is named", missing == ["Critical Context"])


@test
def test_validate_summary_max_tokens_finish_always_fails(ctx: Ctx):
    text = "\n".join(f"## {h}\ncontent" for h in SUMMARY_HEADINGS)  # otherwise-perfect text
    ok, missing = validate_summary(text, stop_reason="max_tokens")
    ctx.check("a max_tokens finish is ALWAYS a failure, even with all headings present", ok is False)
    ok2, _ = validate_summary(text, stop_reason="length")
    ctx.check("'length' finish is also always a failure", ok2 is False)


@test
def test_build_summary_instruction_contains_all_headings_and_custom_text(ctx: Ctx):
    instr = build_summary_instruction("focus on file names")
    for h in SUMMARY_HEADINGS:
        ctx.check(f"instruction names heading {h!r}", f"## {h}" in instr)
    ctx.check("custom instructions included", "focus on file names" in instr)
    ctx.check("forbids mentioning the compaction (dsh rule)", "Do not mention" in instr)


@test
def test_build_summary_instruction_corrective_retry_variant(ctx: Ctx):
    normal = build_summary_instruction()
    corrective = build_summary_instruction(corrective=True, problem="two sections were missing")
    ctx.check("corrective variant is longer", len(corrective) > len(normal))
    ctx.check("corrective variant names the problem", "two sections were missing" in corrective)


@test
def test_wrap_compacted_summary_shape(ctx: Ctx):
    wrapped = wrap_compacted_summary("  the actual summary text  ")
    ctx.check("preamble present", "without acknowledging this checkpoint" in wrapped)
    ctx.check("wrapped in <compacted-summary> tags", "<compacted-summary>" in wrapped and "</compacted-summary>" in wrapped)
    ctx.check("summary text stripped and included", "the actual summary text" in wrapped)


@test
def test_find_prior_summary(ctx: Ctx):
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "no summary here"}]},
        {"role": "user", "content": [{"type": "text", "text": "before <compacted-summary>PRIOR TEXT</compacted-summary> after"}]},
    ]
    found = find_prior_summary(messages)
    ctx.check(f"finds the prior summary block, got {found!r}", found is not None and "PRIOR TEXT" in found)
    ctx.check("returns None when there is none", find_prior_summary(messages[:1]) is None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

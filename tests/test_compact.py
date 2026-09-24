"""tests.test_compact -- agent/compact.py: trigger math (three profiles),
knobs precedence, tail-retention formula, atomic-unit-aware tail selection,
8-heading validation + corrective retry, prior-summary detection."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.agent.compact import (
    SUMMARY_HEADINGS, CompactionKnobs, build_summary_instruction, compaction_trigger_tokens,
    dsh_trigger_tokens, find_prior_summary, opencode_usable, resolve_knobs, select_verbatim_tail,
    should_compact, tail_retention_tokens, validate_summary, wrap_compacted_summary,
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
    # usable = (context - max_output) - min(20_000, max_output)
    u = opencode_usable(200_000, 8_192)
    ctx.check(f"usable matches the formula exactly, got {u}", u == (200_000 - 8_192) - min(20_000, 8_192))
    u2 = opencode_usable(100_000, 50_000)  # reserved caps at 20_000, not 50_000
    ctx.check(f"reserved caps at 20,000 even when max_output is bigger, got {u2}", u2 == (100_000 - 50_000) - 20_000)


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
    class FakeSettings:
        auto_compact_window = None
        auto_compact_enabled = True
        compaction_model = None
        raw = {"compactionModel": "or:some/small-model"}

    knobs = resolve_knobs(FakeSettings(), {})
    ctx.check("compactionModel read from settings.raw", knobs.compaction_model == "or:some/small-model")


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

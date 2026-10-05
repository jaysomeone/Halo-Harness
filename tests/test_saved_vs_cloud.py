"""tests.test_saved_vs_cloud -- Halo 2.0.3 round 5e: the "saved versus
cloud" cost-meter extension. `model.is_local_model_ref`, `model.
catalog_median_prices` (offline-safe: reads the vendored fallback catalogs,
no network), `CostMeter.set_savings_reference`/`add_savings`'s arithmetic,
the extended `/cost` breakdown, and the status bar's " · saved $x" chip
suffix. Hermetic: `catalog_median_prices` reads only package-vendored
files, never `~/.halo`; every other test scopes `BRIDGE_TEST_HOME` itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


@test
def test_is_local_model_ref(ctx: Ctx):
    from halo_harness.model import is_local_model_ref, parse_model_ref
    ctx.check("ol: is local", is_local_model_ref(parse_model_ref("ol:qwen3:30b")))
    ctx.check("hf:local/* is local", is_local_model_ref(parse_model_ref("hf:local/qwen3-30b")))
    ctx.check("hf:mlx/* is local", is_local_model_ref(parse_model_ref("hf:mlx/mlx-community/Qwen2.5-7B-4bit")))
    ctx.check("hf:<org>/<model> (the router, not local) is NOT local",
              not is_local_model_ref(parse_model_ref("hf:deepseek-ai/DeepSeek-V3")))
    ctx.check("or: is not local", not is_local_model_ref(parse_model_ref("or:anthropic/claude-haiku-4.5")))


@test
def test_catalog_median_prices_is_offline_safe_and_real(ctx: Ctx):
    """No network, no BRIDGE_TEST_HOME needed at all -- reads the package-
    vendored fallback catalogs directly. Pinned against the SAME 30-entry
    Databricks fallback file this suite ships with (median input ~$1.325/M,
    output ~$10/M, as of this round -- a future catalog update changing
    these is expected and this test's own job is to catch that, not to
    freeze the file forever)."""
    from halo_harness.model import catalog_median_prices
    med_in, med_out = catalog_median_prices()
    ctx.check(f"a real median input price was found, got {med_in!r}", med_in is not None and med_in > 0)
    ctx.check(f"a real median output price was found, got {med_out!r}", med_out is not None and med_out > 0)
    ctx.check(f"output > input (every priced row here bills output higher), got in={med_in} out={med_out}",
              med_out > med_in)
    med_in2, med_out2, label = catalog_median_prices(source_label=True)
    ctx.check("source_label=True returns the same numbers", (med_in2, med_out2) == (med_in, med_out))
    ctx.check(f"label names a count and 'no network', got {label!r}",
              "priced model" in label and "no network" in label)


@test
def test_cost_meter_add_savings_arithmetic(ctx: Ctx):
    from halo_harness.model import CostMeter
    cm = CostMeter(price_in=0.000001, price_out=0.00001)  # the LOCAL model's own (irrelevant here) price
    cm.set_savings_reference(price_in=0.000002, price_out=0.00003, source="test reference")
    saved = cm.add_savings({"input_tokens": 100, "output_tokens": 50})
    expected = 100 * 0.000002 + 50 * 0.00003
    ctx.check(f"first call returns the exact figure, got {saved} expected {expected}",
              abs(saved - expected) < 1e-12)
    ctx.check(f"running total matches, got {cm.saved_usd}", abs(cm.saved_usd - expected) < 1e-12)
    ctx.check(f"saved_turns incremented, got {cm.saved_turns}", cm.saved_turns == 1)
    saved2 = cm.add_savings({"input_tokens": 10, "output_tokens": 5, "reasoning_tokens": 2,
                              "cache_read_input_tokens": 3, "cache_creation_input_tokens": 1})
    expected2 = 10 * 0.000002 + (5 + 2) * 0.00003 + 3 * 0.000002 + 1 * 0.000002
    ctx.check(f"cache/reasoning tokens priced per the pinned formula, got {saved2} expected {expected2}",
              abs(saved2 - expected2) < 1e-12)
    ctx.check(f"running total accumulates, got {cm.saved_usd}", abs(cm.saved_usd - (expected + expected2)) < 1e-12)
    ctx.check("saved_turns is now 2", cm.saved_turns == 2)


@test
def test_cost_meter_add_savings_is_a_noop_with_no_reference(ctx: Ctx):
    from halo_harness.model import CostMeter
    cm = CostMeter(price_in=0.000001, price_out=0.00001)  # a cloud session: set_savings_reference never called
    result = cm.add_savings({"input_tokens": 1000, "output_tokens": 1000})
    ctx.check("add_savings returns None with no reference price pinned", result is None)
    ctx.check("saved_usd stays 0", cm.saved_usd == 0.0)
    ctx.check("saved_turns stays 0", cm.saved_turns == 0)


@test
def test_cost_meter_add_savings_tolerates_unusable_usage(ctx: Ctx):
    from halo_harness.model import CostMeter
    cm = CostMeter()
    cm.set_savings_reference(price_in=0.000002, price_out=0.00003, source="test")
    ctx.check("non-dict usage -> None, no raise", cm.add_savings(None) is None)
    ctx.check("usage missing token counts -> None, no raise", cm.add_savings({}) is None)
    ctx.check("saved_usd untouched", cm.saved_usd == 0.0)


@test
def test_cmd_cost_includes_saved_breakdown(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_cost
    from halo_harness.model import CostMeter

    class _FakeSession:
        pass

    session = _FakeSession()
    session.cost_meter = CostMeter(price_in=None, price_out=None)
    session.cost_meter.has_cost_data = False
    session.cost_meter.turns = 3
    session.cost_meter.set_savings_reference(price_in=0.000001325, price_out=0.00001, source="catalog median")
    session.cost_meter.add_savings({"input_tokens": 1000, "output_tokens": 1000})
    out = _cmd_cost("", HeadlessFacade(cwd=Path("."), session=session, model_ref="ol:qwen3:30b"))
    ctx.check(f"total cost line present, got {out!r}", "Total cost:" in out)
    ctx.check("saved line present", "Saved vs cloud:" in out)
    ctx.check("reference price shown per-million", "$1.33/M in" in out or "$1.32/M in" in out)
    ctx.check("source shown", "catalog median" in out)


@test
def test_cmd_cost_omits_saved_breakdown_on_cloud_session(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_cost
    from halo_harness.model import CostMeter

    class _FakeSession:
        pass

    session = _FakeSession()
    session.cost_meter = CostMeter(price_in=0.000001, price_out=0.00001)
    session.cost_meter.turns = 1
    out = _cmd_cost("", HeadlessFacade(cwd=Path("."), session=session, model_ref="or:anthropic/claude-haiku-4.5"))
    ctx.check(f"no saved line on a cloud session, got {out!r}", "Saved vs cloud:" not in out)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

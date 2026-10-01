"""tests.test_hotfix_101_fix_cost_accounting -- 1.0.1 fixpass finding 6:
OpenAI-shaped usage (Databricks openai-chat/cursor dialects, OpenRouter) no
longer double-bills cached prompt tokens or reasoning tokens, and `/model`
mid-session refreshes CostMeter's own prices instead of billing NEW usage
at the OLD model's rates forever.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

test, TESTS = new_registry()


class _Env:
    """`Session.__init__` builds a real `SessionLog`, which opens/writes
    under `bridge_home()` the moment the Session exists -- NEVER derived
    from the `state_dir=` constructor kwarg (that's for other session
    state, e.g. tool-result spill files). Without this, a test that never
    sets BRIDGE_TEST_HOME/BRIDGE_STATE_DIR itself silently falls back to
    the REAL ~/.halo/sessions on whatever machine runs the suite."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE")}
        d = Path(tempfile.mkdtemp(prefix="hotfix101-setmodel-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# map_usage: cached/reasoning tokens are SUBSETS of prompt_tokens/
# completion_tokens, never additional tokens layered on top of them.
# ---------------------------------------------------------------------------

@test
def test_map_usage_subtracts_cached_from_input_tokens(ctx: Ctx):
    from halo_harness.providers.oai_stream import map_usage
    u = map_usage({"prompt_tokens": 100, "completion_tokens": 50,
                    "prompt_tokens_details": {"cached_tokens": 40}})
    ctx.check(f"input_tokens is the UNCACHED remainder, got {u}", u["input_tokens"] == 60)
    ctx.check(f"cache_read_input_tokens is the real cached count, got {u}", u["cache_read_input_tokens"] == 40)
    ctx.check(f"input_tokens + cache_read_input_tokens == prompt_tokens (no double count), got {u}",
              u["input_tokens"] + u["cache_read_input_tokens"] == 100)


@test
def test_map_usage_subtracts_reasoning_from_output_tokens(ctx: Ctx):
    from halo_harness.providers.oai_stream import map_usage
    u = map_usage({"prompt_tokens": 100, "completion_tokens": 50,
                    "completion_tokens_details": {"reasoning_tokens": 20}})
    ctx.check(f"output_tokens is the NON-REASONING remainder, got {u}", u["output_tokens"] == 30)
    ctx.check(f"reasoning_tokens is the real reasoning count, got {u}", u["reasoning_tokens"] == 20)
    ctx.check(f"output_tokens + reasoning_tokens == completion_tokens (no double count), got {u}",
              u["output_tokens"] + u["reasoning_tokens"] == 50)


@test
def test_map_usage_unaffected_with_no_cache_or_reasoning_fields(ctx: Ctx):
    from halo_harness.providers.oai_stream import map_usage
    ctx.check("plain mapping is byte-for-byte the same as before this fix",
              map_usage({"prompt_tokens": 10, "completion_tokens": 5}) == {"input_tokens": 10, "output_tokens": 5})


@test
def test_total_prompt_tokens_no_longer_double_counts_cached_tokens(ctx: Ctx):
    """finding 6's own 'related, not new' tail: agent/loop.py's
    _total_prompt_tokens sums input_tokens + cache_read + cache_creation
    on the assumption those are disjoint buckets (true for Anthropic's
    native usage shape already) -- it needs no code change of its own,
    since map_usage's fix (above) is what made that assumption true for
    OpenAI-shaped usage too."""
    from halo_harness.agent.loop import _total_prompt_tokens
    from halo_harness.providers.oai_stream import map_usage
    usage = map_usage({"prompt_tokens": 150_000, "completion_tokens": 2_000,
                        "prompt_tokens_details": {"cached_tokens": 100_000}})
    total = _total_prompt_tokens(usage)
    ctx.check(f"real prompt size (150,000), not double-counted (250,000), got {total}", total == 150_000)


# ---------------------------------------------------------------------------
# CostMeter integration: a cached + reasoning-heavy Databricks gpt-5/6-style
# reply must be billed exactly once per real token, at the right rate.
# ---------------------------------------------------------------------------

@test
def test_cost_meter_does_not_double_bill_cached_and_reasoning_tokens(ctx: Ctx):
    from halo_harness.model import CostMeter
    from halo_harness.providers.oai_stream import map_usage
    # $5/M input, $30/M output, $0.5/M cache-read -- finding 6's own worked
    # example shape (a databricks-gpt-5-6-sol-style reply).
    meter = CostMeter(price_in=5.0 / 1_000_000, price_out=30.0 / 1_000_000,
                       price_cache_read=0.5 / 1_000_000, price_cache_write=None)
    raw = {"prompt_tokens": 150_000, "completion_tokens": 2_000,
           "prompt_tokens_details": {"cached_tokens": 100_000},
           "completion_tokens_details": {"reasoning_tokens": 1_500}}
    usage = map_usage(raw)
    cost = meter.add_usage("databricks", usage)
    # Correct total: 50,000 fresh input @ $5/M + 100,000 cached @ $0.5/M,
    # plus 2,000 output tokens (reasoning already included in that count,
    # billed once at the output rate).
    expected = (50_000 * 5.0 + 100_000 * 0.5) / 1_000_000 + (2_000 * 30.0) / 1_000_000
    ctx.check(f"billed exactly once per real token, got {cost}, expected {expected}",
              cost is not None and abs(cost - expected) < 1e-9)
    # The bug this pins: the OLD (inclusive input/output, additive cache/
    # reasoning) shape overstated this same call by roughly $0.50 (finding
    # 6's own measured figure) -- the fixed cost must land well under what
    # that double-counting formula would have produced.
    old_buggy_cost = (150_000 * 5.0 + 100_000 * 0.5) / 1_000_000 + ((2_000 + 1_500) * 30.0) / 1_000_000
    ctx.check(f"fixed cost ({cost}) is well under the old double-counted figure ({old_buggy_cost})",
              cost < old_buggy_cost - 0.4)


# ---------------------------------------------------------------------------
# Session.set_model refreshes the meter's own prices for a mid-session
# `/model` switch (H14c review finding 6's third bug).
# ---------------------------------------------------------------------------

def _session_for_price_test(model="or:mock/cheap"):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    cwd = Path(tempfile.mkdtemp(prefix="hotfix101-setmodel-"))
    session_ctx = SessionContext(cwd=cwd, model_label=model)
    model_ref = parse_model_ref(model)
    profile = ModelProfile(price_in=1.0 / 1_000_000, price_out=2.0 / 1_000_000)
    return Session(
        cwd=cwd, model_ref=model_ref, model_profile=profile,
        creds=ProviderCreds(base_url="https://example.invalid", api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="hotfix101-setmodel-state-")), model_label=model,
        session_context=session_ctx, max_turns=10,
    )


@test
def test_set_model_refreshes_cost_meter_prices(ctx: Ctx):
    from halo_harness.model import ModelProfile, parse_model_ref
    with _Env():
        session = _session_for_price_test()
        ctx.check("starting price_in is the cheap model's", session.cost_meter.price_in == 1.0 / 1_000_000)
        session.cost_meter.total_usd = 0.0042  # spend already recorded before the switch
        new_ref = parse_model_ref("or:mock/expensive")
        new_profile = ModelProfile(price_in=10.0 / 1_000_000, price_out=20.0 / 1_000_000,
                                    price_cache_read=1.0 / 1_000_000, price_cache_write=3.0 / 1_000_000)
        session.set_model(new_ref, new_profile)
        ctx.check(f"price_in now the NEW model's rate, got {session.cost_meter.price_in}",
                  session.cost_meter.price_in == 10.0 / 1_000_000)
        ctx.check(f"price_out now the NEW model's rate, got {session.cost_meter.price_out}",
                  session.cost_meter.price_out == 20.0 / 1_000_000)
        ctx.check(f"price_cache_read now the NEW model's rate, got {session.cost_meter.price_cache_read}",
                  session.cost_meter.price_cache_read == 1.0 / 1_000_000)
        ctx.check(f"price_cache_write now the NEW model's rate, got {session.cost_meter.price_cache_write}",
                  session.cost_meter.price_cache_write == 3.0 / 1_000_000)
        ctx.check(f"spend already recorded before the switch is untouched, got {session.cost_meter.total_usd}",
                  abs(session.cost_meter.total_usd - 0.0042) < 1e-12)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

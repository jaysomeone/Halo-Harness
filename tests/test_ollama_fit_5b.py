"""tests.test_ollama_fit_5b -- Halo 2.0.3 round 5b: the pure arithmetic
additions (KV-cache-type constants, the multi-GPU fit formula, the
learned-cap/host-max_ctx/remote-default precedence chain) and the stable-
request-prefix pinning test. Never a real OS tool, never a real model,
never the real ~/.halo.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.provider_env_defaults import ensure_default_provider_credentials
ensure_default_provider_credentials()

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


# ---- KV-cache-type constants (GPU-RESEARCH.md's table) -------------------

@test
def test_kv_bytes_per_elem_table_matches_research_doc(ctx: Ctx):
    from halo_harness.providers.ollama_fit import KV_BYTES_PER_ELEM
    # GPU-RESEARCH.md "KV-quantization-per-backend table" -- hand-summed
    # from ggml-common.h's own block struct byte counts (block_size=32).
    expected = {"f32": 4.0, "f16": 2.0, "bf16": 2.0, "q8_0": 1.0625, "q5_1": 0.75,
                "q5_0": 0.6875, "q4_1": 0.625, "q4_0": 0.5625, "iq4_nl": 0.5625}
    for cache_type, bytes_per_elem in expected.items():
        got = KV_BYTES_PER_ELEM.get(cache_type)
        ctx.check(f"{cache_type} == {bytes_per_elem}, got {got}", got == bytes_per_elem)
    ctx.check("q8_0 is NOT the old rounder guess of 1.0 (6% too low)", KV_BYTES_PER_ELEM["q8_0"] != 1.0)
    ctx.check("q4_0 is NOT the old rounder guess of 0.5 (12% too low)", KV_BYTES_PER_ELEM["q4_0"] != 0.5)


@test
def test_kv_bytes_per_elem_for_hint_selects_and_defaults_to_f16(ctx: Ctx):
    from halo_harness.providers.ollama_fit import kv_bytes_per_elem_for
    ctx.check("q8_0 hint", kv_bytes_per_elem_for("q8_0") == 1.0625)
    ctx.check("q4_0 hint, case-insensitive", kv_bytes_per_elem_for("Q4_0") == 0.5625)
    ctx.check("unset (None) defaults to f16", kv_bytes_per_elem_for(None) == 2.0)
    ctx.check("unrecognized string defaults to f16, never raises", kv_bytes_per_elem_for("not-a-real-type") == 2.0)


@test
def test_kv_bytes_per_token_with_explicit_cache_type_hint(ctx: Ctx):
    """The exact figures the KV table drives `kv_bytes_per_token` to --
    same model_info as test_providers_ollama_hw.py's own f16 pin (131072
    bytes/token at f16), scaled by the q8_0/q4_0 constants instead."""
    from halo_harness.providers.ollama_fit import kv_bytes_per_elem_for, kv_bytes_per_token
    model_info = {"qwen3.block_count": 32, "qwen3.attention.head_count_kv": 8,
                  "qwen3.attention.head_count": 32, "qwen3.embedding_length": 4096}
    f16 = kv_bytes_per_token(model_info, bytes_per_elem=kv_bytes_per_elem_for("f16"))
    q8_0 = kv_bytes_per_token(model_info, bytes_per_elem=kv_bytes_per_elem_for("q8_0"))
    q4_0 = kv_bytes_per_token(model_info, bytes_per_elem=kv_bytes_per_elem_for("q4_0"))
    ctx.check(f"f16 baseline, got {f16}", f16 == 131072.0)
    ctx.check(f"q8_0 is exactly 1.0625/2.0 of f16, got {q8_0}", q8_0 == 131072.0 * (1.0625 / 2.0))
    ctx.check(f"q4_0 is exactly 0.5625/2.0 of f16, got {q4_0}", q4_0 == 131072.0 * (0.5625 / 2.0))


# ---- multi-GPU fit formula (sum, not min) ---------------------------------

@test
def test_multi_gpu_fit_estimate_sums_never_takes_minimum(ctx: Ctx):
    """The brief's own "never the minimum" requirement: a weight that
    would NOT fit on the smaller card alone (and barely would on the
    bigger one alone) fits comfortably once both cards' free memory is
    SUMMED -- a min()-based formula would report WEIGHTS_DO_NOT_FIT here
    (min(20 GiB, 1 MiB) is nowhere near 15 GiB), the sum reports a real
    context instead."""
    from halo_harness.providers.ollama_fit import WEIGHTS_DO_NOT_FIT, multi_gpu_fit_estimate
    free_cards = [20 * 1024**3, 1 * 1024 * 1024]  # 20 GiB + a near-empty second card
    weight_bytes = 15 * 1024**3
    got = multi_gpu_fit_estimate(kv_bytes_per_token=4096, free_bytes_per_card=free_cards,
                                  resident_weight_bytes=weight_bytes)
    ctx.check(f"sum (not min) -> a real fit, not WEIGHTS_DO_NOT_FIT, got {got!r}", got is not WEIGHTS_DO_NOT_FIT)
    ctx.check(f"sum (not min) -> a positive power-of-two context, got {got!r}",
              isinstance(got, int) and got > 0)


@test
def test_multi_gpu_fit_estimate_matches_worked_example_shape(ctx: Ctx):
    """GPU-RESEARCH.md's own worked example shape (illustrative numbers,
    not any real host's): two cards, free_total = sum(frees) -
    overhead*len(cards); headroom = free_total - weight; same power-of-
    two floor fit_estimate() already uses for one card."""
    from halo_harness.providers.ollama_fit import multi_gpu_fit_estimate
    free_cards = [22 * 1024**3, 11 * 1024**3]  # 33 GiB combined, the doc's own numbers
    weight_bytes = 18 * 1024**3
    kv = 4096
    got = multi_gpu_fit_estimate(kv_bytes_per_token=kv, free_bytes_per_card=free_cards,
                                  resident_weight_bytes=weight_bytes)
    headroom = sum(free_cards) - weight_bytes  # 15 GiB
    expected = 1
    while expected * 2 <= headroom // kv:
        expected *= 2
    ctx.check(f"33 GiB - 18 GiB headroom, floored to a power of two, got {got} expected {expected}",
              got == expected)


@test
def test_multi_gpu_fit_estimate_overhead_is_per_card_not_flat(ctx: Ctx):
    """OLLAMA_GPU_OVERHEAD's own doc comment: "set aside VRAM per GPU" --
    a PER-CARD reserve, so two cards take out twice the overhead, not
    once. Clean round numbers chosen so the effect is exactly checkable."""
    from halo_harness.providers.ollama_fit import multi_gpu_fit_estimate
    kv = 1  # 1 byte/token -- isolates the headroom-in-bytes arithmetic
    free_cards = [1000, 1000]
    weight_bytes = 100
    no_overhead = multi_gpu_fit_estimate(kv_bytes_per_token=kv, free_bytes_per_card=free_cards,
                                          resident_weight_bytes=weight_bytes, overhead_per_card_bytes=0)
    with_overhead = multi_gpu_fit_estimate(kv_bytes_per_token=kv, free_bytes_per_card=free_cards,
                                            resident_weight_bytes=weight_bytes, overhead_per_card_bytes=100)
    # no_overhead headroom = 2000 - 100 = 1900 -> floor(1900) power of two = 1024
    # with_overhead headroom = 2000 - 2*100 - 100 = 1700 -> floor(1700) power of two = 1024 too close to
    # distinguish by final power-of-two; use a kv_bytes_per_token that makes the difference cross a boundary.
    ctx.check(f"overhead strictly reduces (or keeps equal, never increases) the result, "
              f"got no_overhead={no_overhead} with_overhead={with_overhead}",
              with_overhead <= no_overhead)
    # A sharper check: 512 tokens/byte-ish boundary crossing.
    kv2 = 1
    free2 = [612, 612]  # combined 1224
    weight2 = 100
    tight_no_overhead = multi_gpu_fit_estimate(kv_bytes_per_token=kv2, free_bytes_per_card=free2,
                                                resident_weight_bytes=weight2, overhead_per_card_bytes=0)
    tight_with_overhead = multi_gpu_fit_estimate(kv_bytes_per_token=kv2, free_bytes_per_card=free2,
                                                  resident_weight_bytes=weight2, overhead_per_card_bytes=200)
    # no overhead: headroom = 1224-100=1124 -> floor pow2 = 1024
    # with 200/card overhead (400 total): headroom = 1224-400-100=724 -> floor pow2 = 512
    ctx.check(f"no-overhead headroom floors to 1024, got {tight_no_overhead}", tight_no_overhead == 1024)
    ctx.check(f"200/card overhead (400 total) pushes the floor down to 512, got {tight_with_overhead}",
              tight_with_overhead == 512)


@test
def test_multi_gpu_fit_estimate_single_card_matches_fit_estimate(ctx: Ctx):
    """"single-card hosts keep calling fit_estimate unchanged, since the
    sum-of-one-card case is identical to today's formula" -- pinned
    directly: multi_gpu_fit_estimate([one card]) == fit_estimate(that
    one card's free bytes)."""
    from halo_harness.providers.ollama_fit import fit_estimate, multi_gpu_fit_estimate
    kv, free_bytes, weight_bytes = 4096, 20 * 1024**3, 4 * 1024**3
    multi = multi_gpu_fit_estimate(kv_bytes_per_token=kv, free_bytes_per_card=[free_bytes],
                                    resident_weight_bytes=weight_bytes)
    single = fit_estimate(kv_bytes_per_token=kv, free_memory_bytes=free_bytes, resident_weight_bytes=weight_bytes)
    ctx.check(f"one-card multi_gpu_fit_estimate == fit_estimate, got {multi} vs {single}", multi == single)


@test
def test_multi_gpu_fit_estimate_unknown_inputs_degrade_to_none(ctx: Ctx):
    from halo_harness.providers.ollama_fit import multi_gpu_fit_estimate
    ctx.check("empty card list -> None", multi_gpu_fit_estimate(
        kv_bytes_per_token=1, free_bytes_per_card=[], resident_weight_bytes=1) is None)
    ctx.check("non-list -> None", multi_gpu_fit_estimate(
        kv_bytes_per_token=1, free_bytes_per_card=None, resident_weight_bytes=1) is None)
    ctx.check("a negative card entry -> None (never a guess)", multi_gpu_fit_estimate(
        kv_bytes_per_token=1, free_bytes_per_card=[-1, 100], resident_weight_bytes=1) is None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

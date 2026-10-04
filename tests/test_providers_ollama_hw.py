"""tests.test_providers_ollama_hw -- Halo 2.0.3 round 3: the pure fit-
estimate/tools_max arithmetic (providers.ollama_fit) and the hardware
vendor probes (providers.ollama_hw), pinned with injected `runner`
callables -- never a real OS tool, never the real ~/.halo. Panel-
rendering tests (analyze_host/format_host_analysis) live in
tests/test_providers_ollama_panel.py; `estimate_fit_for_host`'s own
`/api/ps`-consulting tests live HERE (below, mock_ollama-backed) since
that function itself lives in providers.ollama_hw.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.provider_env_defaults import ensure_default_provider_credentials
ensure_default_provider_credentials()

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_ollama import MockUpstream, partial_offload_ps_entry

test, TESTS = new_registry()

_NO_GPU_RUNNER = lambda argv, timeout: None  # never a real OS tool in this file


def _catalog_for(model: str, *, size: int, model_info: dict) -> dict:
    """A minimal `get_catalog()`-shaped dict with one row -- just enough
    for `catalog_row`/`kv_bytes_per_token` to read from."""
    return {"models": [{"model": model, "name": model, "size": size, "model_info": model_info}]}


_QWEN_MODEL_INFO = {"qwen3.block_count": 32, "qwen3.attention.head_count_kv": 8,
                    "qwen3.attention.head_count": 32, "qwen3.embedding_length": 4096}  # 131072 bytes/token, f16


def _fresh_state_dir(prefix: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    return d


def _clear_state_dir_env() -> None:
    os.environ.pop("BRIDGE_STATE_DIR", None)


# ---- tools_max classes -------------------------------------------------

@test
def test_tools_max_classes_floor_and_boundaries(ctx: Ctx):
    from halo_harness.providers.ollama_fit import tools_max_for_num_ctx
    cases = [(None, 16), (1, 16), (16_383, 16), (16_384, 32), (32_767, 32),
              (32_768, 64), (65_535, 64), (65_536, 128), (1_000_000, 128)]
    for num_ctx, expected in cases:
        got = tools_max_for_num_ctx(num_ctx, floor=0)
        ctx.check(f"tools_max_for_num_ctx({num_ctx!r}, floor=0) == {expected}, got {got}", got == expected)


@test
def test_tools_max_never_below_floor(ctx: Ctx):
    from halo_harness.providers.ollama_fit import tools_max_for_num_ctx
    ctx.check("a tiny class value is still raised to a bigger floor",
              tools_max_for_num_ctx(1, floor=999) == 999)
    ctx.check("the real default floor is >= 1 (this platform's own built-in count)",
              tools_max_for_num_ctx(None) >= 1)


@test
def test_ollama_tools_max_config_override(ctx: Ctx):
    from halo_harness.theme import set_config_value
    from halo_harness.providers.ollama_fit import resolve_ollama_tools_max
    _fresh_state_dir("ol-tools-max-override-")
    try:
        set_config_value("ollama.tools_max", 40)
        ctx.check("override wins over the context class", resolve_ollama_tools_max(1_000_000) == 40)
        set_config_value("ollama.tools_max", 1)
        ctx.check("override still never drops below floor", resolve_ollama_tools_max(1, floor=50) == 50)
    finally:
        _clear_state_dir_env()


# ---- KV bytes/token -----------------------------------------------------

@test
def test_kv_bytes_per_token_formula(ctx: Ctx):
    from halo_harness.providers.ollama_fit import kv_bytes_per_token
    model_info = {"qwen3.block_count": 32, "qwen3.attention.head_count_kv": 8,
                  "qwen3.attention.head_count": 32, "qwen3.embedding_length": 4096}
    # head_dim = 4096/32 = 128; 2 * 32 * 8 * 128 * 2.0(f16) = 131072.0
    got = kv_bytes_per_token(model_info)
    ctx.check(f"f16 KV bytes/token == 131072.0, got {got}", got == 131072.0)
    got_q4 = kv_bytes_per_token(model_info, bytes_per_elem=0.5)
    ctx.check(f"q4_0 KV bytes/token == 32768.0, got {got_q4}", got_q4 == 32768.0)


@test
def test_kv_bytes_per_token_missing_field_is_none(ctx: Ctx):
    from halo_harness.providers.ollama_fit import kv_bytes_per_token
    ctx.check("empty model_info -> None", kv_bytes_per_token({}) is None)
    ctx.check("missing embedding_length -> None",
              kv_bytes_per_token({"x.block_count": 1, "x.attention.head_count_kv": 1}) is None)


# ---- pure fit_estimate ---------------------------------------------------

@test
def test_fit_estimate_fits_with_room_rounds_to_power_of_two(ctx: Ctx):
    from halo_harness.providers.ollama_fit import fit_estimate
    got = fit_estimate(kv_bytes_per_token=4096, free_memory_bytes=20 * 1024**3, resident_weight_bytes=4 * 1024**3)
    headroom = 20 * 1024**3 - 4 * 1024**3
    expected = 1
    while expected * 2 <= headroom // 4096:
        expected *= 2
    ctx.check(f"largest power of two that fits, got {got} expected {expected}", got == expected)


@test
def test_fit_estimate_weights_exceed_free_memory_is_weights_do_not_fit(ctx: Ctx):
    """Round 3 fix pass: this used to return bare `None`, indistinguishable
    from "unknown" -- `compute_num_ctx` then let the 131072 hard cap win
    for a model whose weights don't even fit, instead of a conservative
    fallback. Now a NAMED sentinel, never a bare None/0/negative number."""
    from halo_harness.providers.ollama_fit import WEIGHTS_DO_NOT_FIT, fit_estimate
    got = fit_estimate(kv_bytes_per_token=4096, free_memory_bytes=4 * 1024**3, resident_weight_bytes=8 * 1024**3)
    ctx.check(f"partial-offload situation -> WEIGHTS_DO_NOT_FIT, got {got!r}", got is WEIGHTS_DO_NOT_FIT)


@test
def test_fit_estimate_unknown_inputs_are_none(ctx: Ctx):
    from halo_harness.providers.ollama_fit import fit_estimate
    ctx.check("kv unknown", fit_estimate(kv_bytes_per_token=None, free_memory_bytes=1, resident_weight_bytes=1) is None)
    ctx.check("free unknown", fit_estimate(kv_bytes_per_token=1, free_memory_bytes=None, resident_weight_bytes=1) is None)
    ctx.check("zero kv", fit_estimate(kv_bytes_per_token=0, free_memory_bytes=1, resident_weight_bytes=1) is None)


@test
def test_remote_loaded_context_as_fit_estimate(ctx: Ctx):
    from halo_harness.providers.ollama_fit import remote_loaded_context_as_fit_estimate
    ctx.check("40000 rounds down to 32768", remote_loaded_context_as_fit_estimate({"context_length": 40000}) == 32768)
    ctx.check("not loaded -> None", remote_loaded_context_as_fit_estimate(None) is None)
    ctx.check("no context_length field -> None", remote_loaded_context_as_fit_estimate({}) is None)


@test
def test_compute_num_ctx_weights_do_not_fit_uses_conservative_fallback_not_hard_cap(ctx: Ctx):
    """Round 3 fix pass pin: a 27B model's trained context (262144) must
    no longer let the 131072 hard cap win when its own weights are known
    not to fit -- 8192 (the conservative fallback) wins instead."""
    from halo_harness.providers.ollama import compute_num_ctx
    from halo_harness.providers.ollama_fit import WEIGHTS_DO_NOT_FIT
    got = compute_num_ctx(262144, None, WEIGHTS_DO_NOT_FIT)
    ctx.check(f"falls back to 8192, not the 131072 hard cap, got {got}", got == 8192)
    got_override = compute_num_ctx(262144, 4096, WEIGHTS_DO_NOT_FIT)
    ctx.check(f"an explicit smaller host_max_ctx still wins over the fallback, got {got_override}",
              got_override == 4096)
    got_unknown = compute_num_ctx(262144, None, None)
    ctx.check(f"plain None is UNCHANGED -- the hard cap still stands, got {got_unknown}", got_unknown == 131072)


# ---- estimate_fit_for_host: /api/ps consulted first (round 3 fix pass) --

@test
def test_estimate_fit_for_host_loaded_model_returns_loaded_context(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import estimate_fit_for_host
    mock = MockUpstream(ps_response={"models": [
        partial_offload_ps_entry("qwen3:30b", size=20 * 1024**3, size_vram=12 * 1024**3, context_length=40960),
    ]}).start()
    try:
        host = OllamaHost(name="mock", url=mock.base_url)
        catalog = _catalog_for("qwen3:30b", size=19 * 1024**3, model_info=_QWEN_MODEL_INFO)
        got = estimate_fit_for_host(host, "qwen3:30b", catalog, runner=_NO_GPU_RUNNER)
        ctx.check(f"the loaded context_length (40960), floored to 32768, got {got}", got == 32768)
    finally:
        mock.stop()


@test
def test_estimate_fit_for_host_second_call_matches_first_even_if_gpu_reading_moved(ctx: Ctx):
    """The exact live-run bug: two readings of free VRAM a bit apart used
    to compute two DIFFERENT num_ctx values for the SAME loaded model.
    The GPU runner here deliberately returns a different free-memory
    reading on each call -- the result must stay identical regardless,
    because a loaded model never even reaches the GPU arithmetic."""
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import estimate_fit_for_host
    mock = MockUpstream(ps_response={"models": [
        partial_offload_ps_entry("qwen3:30b", size=20 * 1024**3, size_vram=12 * 1024**3, context_length=40960),
    ]}).start()
    calls = {"n": 0}

    def moving_runner(argv, timeout):
        calls["n"] += 1
        return f"{100 + calls['n']}, 10, {90 - calls['n']}, X\n"
    try:
        host = OllamaHost(name="mock", url=mock.base_url)
        catalog = _catalog_for("qwen3:30b", size=19 * 1024**3, model_info=_QWEN_MODEL_INFO)
        first = estimate_fit_for_host(host, "qwen3:30b", catalog, runner=moving_runner)
        second = estimate_fit_for_host(host, "qwen3:30b", catalog, runner=moving_runner)
        ctx.check(f"no thrash: both calls equal, got {first} then {second}", first == second == 32768)
        ctx.check("the GPU runner was never even invoked for a loaded model", calls["n"] == 0)
    finally:
        mock.stop()


@test
def test_estimate_fit_for_host_other_loaded_models_count_as_reclaimable_headroom(ctx: Ctx):
    """Not loaded itself, but ANOTHER model occupies VRAM Ollama could
    evict to make room -- that size_vram must count toward THIS model's
    own headroom, not just the GPU's own already-free bytes."""
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import estimate_fit_for_host
    weight_bytes = 18 * 1024**3
    other_size_vram = 20 * 1024**3
    free_bytes = 1 * 1024**3  # alone, far too little for an 18 GB model
    mock = MockUpstream(ps_response={"models": [
        partial_offload_ps_entry("other-model", size=other_size_vram, size_vram=other_size_vram,
                                  context_length=8192),
    ]}).start()

    def runner(argv, timeout):
        return f"{(free_bytes + weight_bytes) // (1024 * 1024)}, 0, {free_bytes // (1024 * 1024)}, X\n"
    try:
        host = OllamaHost(name="mock", url=mock.base_url)
        catalog = _catalog_for("qwen3:30b", size=weight_bytes, model_info=_QWEN_MODEL_INFO)
        got = estimate_fit_for_host(host, "qwen3:30b", catalog, runner=runner)
        headroom = (free_bytes + other_size_vram) - weight_bytes
        expected = 1
        while expected * 2 <= headroom // 131072:
            expected *= 2
        ctx.check(f"reclaimable headroom from the other loaded model produces a real fit estimate, "
                  f"got {got} expected {expected}", got == expected)
    finally:
        mock.stop()


@test
def test_estimate_fit_for_host_weights_still_do_not_fit_after_reclaiming(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_fit import WEIGHTS_DO_NOT_FIT
    from halo_harness.providers.ollama_hw import estimate_fit_for_host
    mock = MockUpstream(ps_response={"models": [
        partial_offload_ps_entry("other-model", size=2 * 1024**3, size_vram=2 * 1024**3, context_length=8192),
    ]}).start()

    def runner(argv, timeout):
        return "2048, 0, 1024, X\n"  # 1 GiB free, 2 GiB other -- nowhere near a 40 GB model
    try:
        host = OllamaHost(name="mock", url=mock.base_url)
        catalog = _catalog_for("huge-model", size=40 * 1024**3, model_info=_QWEN_MODEL_INFO)
        got = estimate_fit_for_host(host, "huge-model", catalog, runner=runner)
        ctx.check(f"still does not fit even after reclaiming, got {got!r}", got is WEIGHTS_DO_NOT_FIT)
    finally:
        mock.stop()


@test
def test_estimate_fit_for_host_remote_path_unchanged(ctx: Ctx):
    """A non-local host still never gets an OS-level read -- only
    `/api/ps` -- exactly as before this fix pass; patches `providers.
    ollama.fetch_ps` directly (hermetic: `.example` never resolves, so a
    real request would just time out instead of proving anything)."""
    import halo_harness.providers.ollama as ollama_mod
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import estimate_fit_for_host
    host = OllamaHost(name="remote", url="http://gpu-box.example:11434")
    catalog = _catalog_for("qwen3:30b", size=19 * 1024**3, model_info=_QWEN_MODEL_INFO)
    old_fetch_ps = ollama_mod.fetch_ps
    ollama_mod.fetch_ps = lambda h: {"models": [
        {"model": "qwen3:30b", "size": 20 * 1024**3, "size_vram": 12 * 1024**3, "context_length": 40960}]}
    try:
        got = estimate_fit_for_host(host, "qwen3:30b", catalog, runner=_NO_GPU_RUNNER)
        ctx.check(f"remote loaded model -> its own context_length floored, got {got}", got == 32768)
        ollama_mod.fetch_ps = lambda h: {"models": []}
        got_absent = estimate_fit_for_host(host, "qwen3:30b", catalog, runner=_NO_GPU_RUNNER)
        ctx.check(f"remote, not loaded -> None (no GPU read ever attempted), got {got_absent!r}",
                  got_absent is None)
    finally:
        ollama_mod.fetch_ps = old_fetch_ps


# ---- GPU vendor probes (injected runner -- never a real OS tool) --------

@test
def test_probe_nvidia_parses_csv_noheader_nounits(ctx: Ctx):
    from halo_harness.providers.ollama_hw import probe_local_gpu_memory
    def runner(argv, timeout):
        ctx.check("nvidia-smi invoked with the verified csv,noheader,nounits flags",
                  "--format=csv,noheader,nounits" in argv)
        return "23028, 1304, 21305, Mock GPU\n"
    gpu = probe_local_gpu_memory(runner=runner)
    ctx.check("vendor nvidia", gpu.vendor == "nvidia")
    ctx.check("name parsed", gpu.name == "Mock GPU")
    ctx.check(f"total bytes, got {gpu.total_bytes}", gpu.total_bytes == 23028 * 1024 * 1024)
    ctx.check(f"free bytes, got {gpu.free_bytes}", gpu.free_bytes == 21305 * 1024 * 1024)


@test
def test_probe_returns_none_on_missing_tool(ctx: Ctx):
    from halo_harness.providers.ollama_hw import probe_local_gpu_memory
    gpu = probe_local_gpu_memory(runner=lambda argv, timeout: None)
    ctx.check("every vendor probe failing -> None, never a guess", gpu is None)


@test
def test_is_local_host_recognizes_loopback_only(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import is_local_host
    ctx.check("127.0.0.1 is local", is_local_host(OllamaHost(name="a", url="http://127.0.0.1:11434")))
    ctx.check("localhost is local", is_local_host(OllamaHost(name="a", url="http://localhost:11434")))
    ctx.check("a remote host is not local",
              not is_local_host(OllamaHost(name="a", url="http://gpu-box.example:11434")))


@test
def test_get_local_gpu_memory_cached_and_respects_no_background_net(ctx: Ctx):
    from halo_harness.providers import ollama_hw
    ollama_hw.reset_local_gpu_cache()
    calls = {"n": 0}
    def runner(argv, timeout):
        calls["n"] += 1
        return "100, 10, 90, X\n"
    first = ollama_hw.get_local_gpu_memory(runner=runner)
    second = ollama_hw.get_local_gpu_memory(runner=runner)
    ctx.check("same cached GpuMemory object across two calls within the TTL", first is second)
    ctx.check("the OS tool was shelled out to exactly once", calls["n"] == 1)
    ollama_hw.reset_local_gpu_cache()
    old = os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET")
    os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"
    try:
        ctx.check("no runner given + BRIDGE_TEST_NO_BACKGROUND_NET=1 -> never shells out, returns None",
                  ollama_hw.get_local_gpu_memory() is None)
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        else:
            os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = old
        ollama_hw.reset_local_gpu_cache()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

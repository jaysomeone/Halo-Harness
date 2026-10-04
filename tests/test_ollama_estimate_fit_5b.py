"""tests.test_ollama_estimate_fit_5b -- Halo 2.0.3 round 5b: the three
new inputs wired INTO `providers.ollama_hw.estimate_fit_for_host` itself
(not just the pure functions they call) -- `ollama.hosts[].kv_cache_type`,
multi-GPU (several cards probed locally), and `ollama.hosts[].ssh` for a
remote host. Against `tests/helpers/mock_ollama.MockUpstream` with
injected `runner`s only -- never a real OS tool, never a real ssh binary.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.provider_env_defaults import ensure_default_provider_credentials
ensure_default_provider_credentials()

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_ollama import MockUpstream

test, TESTS = new_registry()

_QWEN_MODEL_INFO = {"qwen3.block_count": 32, "qwen3.attention.head_count_kv": 8,
                    "qwen3.attention.head_count": 32, "qwen3.embedding_length": 4096}  # 131072 B/tok at f16


def _catalog_for(model: str, *, size: int) -> dict:
    return {"models": [{"model": model, "name": model, "size": size, "model_info": _QWEN_MODEL_INFO}]}


def _reset():
    from halo_harness.providers import ollama_hw
    ollama_hw.reset_local_gpu_cache()
    ollama_hw.reset_ssh_gpu_cache()


# ---- kv_cache_type hint -----------------------------------------------

@test
def test_estimate_fit_for_host_uses_kv_cache_type_hint(ctx: Ctx):
    """A q4_0 host fits a MUCH larger context than an f16 host with the
    EXACT same free memory/weight size -- the hint must actually change
    the bytes-per-token constant `estimate_fit_for_host` uses, not just
    exist on the dataclass."""
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import estimate_fit_for_host
    mock = MockUpstream(ps_response={"models": []}).start()
    _reset()
    try:
        catalog = _catalog_for("qwen3:30b", size=1 * 1024**3)

        def runner(argv, timeout):
            return "20480, 0, 20480, X\n"  # 20 GiB free, one card
        host_f16 = OllamaHost(name="mock", url=mock.base_url)
        host_q4 = OllamaHost(name="mock", url=mock.base_url, kv_cache_type="q4_0")
        fit_f16 = estimate_fit_for_host(host_f16, "qwen3:30b", catalog, runner=runner)
        _reset()
        fit_q4 = estimate_fit_for_host(host_q4, "qwen3:30b", catalog, runner=runner)
        ctx.check(f"q4_0 fits a bigger context than f16 with identical memory, got f16={fit_f16} q4_0={fit_q4}",
                  isinstance(fit_f16, int) and isinstance(fit_q4, int) and fit_q4 > fit_f16)
    finally:
        mock.stop()
        _reset()


# ---- multi-GPU wiring --------------------------------------------------

@test
def test_estimate_fit_for_host_multi_gpu_sums_not_minimum(ctx: Ctx):
    """Two cards probed locally -- the SAME "never the minimum" property
    `test_ollama_fit_5b.py` already pins for the pure formula, now
    through the real `estimate_fit_for_host` entry point a request
    actually uses."""
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import estimate_fit_for_host
    from halo_harness.providers.ollama_fit import WEIGHTS_DO_NOT_FIT
    mock = MockUpstream(ps_response={"models": []}).start()
    _reset()
    try:
        weight_bytes = 15 * 1024**3
        catalog = _catalog_for("qwen3:30b", size=weight_bytes)

        def runner(argv, timeout):
            return "20480, 0, 20480, Card A\n1, 0, 1, Card B\n"  # 20 GiB + ~1 MiB
        host = OllamaHost(name="mock", url=mock.base_url)
        got = estimate_fit_for_host(host, "qwen3:30b", catalog, runner=runner)
        ctx.check(f"sums both cards -> a real fit, not WEIGHTS_DO_NOT_FIT, got {got!r}", got is not None
                  and got is not WEIGHTS_DO_NOT_FIT)
    finally:
        mock.stop()
        _reset()


@test
def test_estimate_fit_for_host_multi_gpu_reclaimable_counts_once(ctx: Ctx):
    """Another loaded model's reclaimable `size_vram` still counts toward
    the TOTAL across every card -- not lost, not double-counted."""
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import estimate_fit_for_host
    from tests.helpers.mock_ollama import partial_offload_ps_entry
    other_vram = 10 * 1024**3
    mock = MockUpstream(ps_response={"models": [
        partial_offload_ps_entry("other-model", size=other_vram, size_vram=other_vram, context_length=8192),
    ]}).start()
    _reset()
    try:
        weight_bytes = 15 * 1024**3
        catalog = _catalog_for("qwen3:30b", size=weight_bytes)

        def runner(argv, timeout):
            return "6144, 0, 6144, Card A\n1, 0, 1, Card B\n"  # only ~6 GiB free alone
        host = OllamaHost(name="mock", url=mock.base_url)
        got = estimate_fit_for_host(host, "qwen3:30b", catalog, runner=runner)
        ctx.check(f"6 GiB + 10 GiB reclaimable >= 15 GiB weight -- fits, got {got!r}",
                  isinstance(got, int) and got > 0)
    finally:
        mock.stop()
        _reset()


# ---- optional ssh GPU read for a remote host --------------------------

@test
def test_estimate_fit_for_host_remote_with_ssh_uses_the_ssh_probe(ctx: Ctx):
    """`host.url` must be a NON-loopback hostname for `is_local_host` to
    take the remote branch at all -- following `test_providers_ollama_hw.
    py::test_estimate_fit_for_host_remote_path_unchanged`'s own pattern,
    `providers.ollama.fetch_ps` is monkeypatched directly (`.example`
    never resolves, so a real request would just time out instead of
    proving anything) rather than spinning a loopback mock server, which
    `is_local_host` would misclassify as local."""
    import halo_harness.providers.ollama as ollama_mod
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import estimate_fit_for_host
    _reset()
    old_fetch_ps = ollama_mod.fetch_ps
    ollama_mod.fetch_ps = lambda h, **kw: {"models": []}  # nothing loaded remotely
    try:
        catalog = _catalog_for("qwen3:30b", size=4 * 1024**3)
        host = OllamaHost(name="remote", url="http://gpu-box.example:11434", ssh="user@gpu-box")

        def runner(argv, timeout):
            return "20480, 0, 20480, Remote GPU\n"
        got = estimate_fit_for_host(host, "qwen3:30b", catalog, runner=runner)
        ctx.check(f"a real fit via the ssh probe, got {got!r}", isinstance(got, int) and got > 0)
    finally:
        ollama_mod.fetch_ps = old_fetch_ps
        _reset()


@test
def test_estimate_fit_for_host_remote_without_ssh_unchanged(ctx: Ctx):
    """No `ssh:` configured -- a remote host with nothing loaded still
    gets `None` (never an OS-level read), unchanged from before round 5b."""
    import halo_harness.providers.ollama as ollama_mod
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_hw import estimate_fit_for_host
    old_fetch_ps = ollama_mod.fetch_ps
    ollama_mod.fetch_ps = lambda h, **kw: {"models": []}
    try:
        catalog = _catalog_for("qwen3:30b", size=4 * 1024**3)
        host = OllamaHost(name="remote", url="http://gpu-box.example:11434")  # no ssh
        got = estimate_fit_for_host(host, "qwen3:30b", catalog,
                                     runner=lambda argv, timeout: "20480, 0, 20480, X\n")
        ctx.check(f"None -- the runner is never even consulted for a plain remote host, got {got!r}", got is None)
    finally:
        ollama_mod.fetch_ps = old_fetch_ps


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

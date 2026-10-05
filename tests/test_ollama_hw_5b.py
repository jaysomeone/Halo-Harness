"""tests.test_ollama_hw_5b -- Halo 2.0.3 round 5b: the multi-GPU probe
(`_probe_nvidia(probe_all=True)`/`get_local_gpu_memories`), the Apple
unified-memory probe and its fraction arithmetic, the optional ssh GPU
transport, and the `last_known_offload` cache. Every GPU read is an
injected `runner` callable -- never a real OS tool, never a real ssh
binary. `ollama_hw.reset_local_gpu_cache()`/`reset_ssh_gpu_cache()` are
called around every test that uses a MEANINGFUL (non-None-returning)
runner, since both caches are process-wide (shared with every other
test_*.py module `tests/run_all.py` imports into the same process).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.provider_env_defaults import ensure_default_provider_credentials
ensure_default_provider_credentials()

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _reset_caches():
    from halo_harness.providers import ollama_hw
    ollama_hw.reset_local_gpu_cache()
    ollama_hw.reset_ssh_gpu_cache()


# ---- multi-GPU probe -------------------------------------------------------

@test
def test_probe_nvidia_probe_all_returns_every_card(ctx: Ctx):
    from halo_harness.providers.ollama_hw import _probe_nvidia
    csv = "23028, 1304, 21305, Card A\n12000, 500, 11000, Card B\n"
    cards = _probe_nvidia(timeout=1.0, runner=lambda argv, timeout: csv, probe_all=True)
    ctx.check(f"two cards parsed, got {cards}", len(cards) == 2)
    ctx.check("card A name", cards[0].name == "Card A")
    ctx.check("card B name", cards[1].name == "Card B")
    ctx.check(f"card A free bytes, got {cards[0].free_bytes}", cards[0].free_bytes == 21305 * 1024 * 1024)
    ctx.check(f"card B free bytes, got {cards[1].free_bytes}", cards[1].free_bytes == 11000 * 1024 * 1024)


@test
def test_probe_nvidia_probe_all_empty_on_missing_tool(ctx: Ctx):
    from halo_harness.providers.ollama_hw import _probe_nvidia
    cards = _probe_nvidia(timeout=1.0, runner=lambda argv, timeout: None, probe_all=True)
    ctx.check(f"[] (never None) on total failure, got {cards!r}", cards == [])


@test
def test_probe_nvidia_default_still_single_card_unchanged(ctx: Ctx):
    """probe_all defaults to False -- the pre-5b single-GpuMemory-or-None
    contract every existing caller/test already depends on."""
    from halo_harness.providers.ollama_hw import _probe_nvidia
    csv = "23028, 1304, 21305, Card A\n12000, 500, 11000, Card B\n"
    gpu = _probe_nvidia(timeout=1.0, runner=lambda argv, timeout: csv)
    ctx.check("a single GpuMemory, not a list", gpu.__class__.__name__ == "GpuMemory")
    ctx.check("the FIRST card only", gpu.name == "Card A")


@test
def test_get_local_gpu_memories_multi_card(ctx: Ctx):
    from halo_harness.providers import ollama_hw
    _reset_caches()
    try:
        csv = "23028, 1304, 21305, Card A\n12000, 500, 11000, Card B\n"
        cards = ollama_hw.get_local_gpu_memories(runner=lambda argv, timeout: csv)
        ctx.check(f"two cards cached, got {len(cards)}", len(cards) == 2)
    finally:
        _reset_caches()


@test
def test_get_local_gpu_memories_single_card_matches_get_local_gpu_memory(ctx: Ctx):
    from halo_harness.providers import ollama_hw
    _reset_caches()
    try:
        csv = "23028, 1304, 21305, Mock GPU\n"
        multi = ollama_hw.get_local_gpu_memories(runner=lambda argv, timeout: csv)
        ctx.check(f"one card, got {len(multi)}", len(multi) == 1)
        ctx.check("matches the single-card probe's own reading", multi[0].free_bytes == 21305 * 1024 * 1024)
    finally:
        _reset_caches()


@test
def test_get_local_gpu_memories_empty_on_no_gpu(ctx: Ctx):
    from halo_harness.providers import ollama_hw
    _reset_caches()
    try:
        cards = ollama_hw.get_local_gpu_memories(runner=lambda argv, timeout: None)
        ctx.check(f"[] (never None) when nothing is found, got {cards!r}", cards == [])
    finally:
        _reset_caches()


# ---- Apple unified memory ---------------------------------------------------

@test
def test_apple_unified_memory_uses_fraction_when_wired_limit_unset(ctx: Ctx):
    from halo_harness.providers.ollama_hw import APPLE_GPU_SHARE_FRACTION_DEFAULT, probe_apple_unified_memory
    total = 36 * 1024**3  # 36 GiB, GPU-RESEARCH.md's own worked-example number

    def runner(argv, timeout):
        if argv[-1] == "hw.memsize":
            return f"{total}\n"
        return ""  # iogpu.wired_limit_mb unset
    gpu = probe_apple_unified_memory(timeout=1.0, runner=runner)
    ctx.check("vendor apple", gpu.vendor == "apple")
    ctx.check(f"total bytes, got {gpu.total_bytes}", gpu.total_bytes == total)
    ctx.check(f"free bytes is the fraction of total, got {gpu.free_bytes}",
              gpu.free_bytes == int(total * APPLE_GPU_SHARE_FRACTION_DEFAULT))
    ctx.check("labelled an estimate (the fraction is UNCONFIRMED exact)", gpu.estimated is True)


@test
def test_apple_unified_memory_exact_when_wired_limit_set(ctx: Ctx):
    """`sudo sysctl iogpu.wired_limit_mb=<N>` (mlx-lm's own documented
    override) REPLACES the fraction entirely -- exact, never an estimate."""
    from halo_harness.providers.ollama_hw import probe_apple_unified_memory
    total = 36 * 1024**3
    wired_mb = 30 * 1024  # 30 GiB, the research doc's own worked-example override

    def runner(argv, timeout):
        if argv[-1] == "hw.memsize":
            return f"{total}\n"
        return f"{wired_mb}\n"
    gpu = probe_apple_unified_memory(timeout=1.0, runner=runner)
    ctx.check(f"free bytes is EXACTLY the wired limit, got {gpu.free_bytes}", gpu.free_bytes == wired_mb * 1024 * 1024)
    ctx.check("never labelled an estimate once the real override is set", gpu.estimated is False)


@test
def test_apple_unified_memory_none_when_hw_memsize_unreadable(ctx: Ctx):
    from halo_harness.providers.ollama_hw import probe_apple_unified_memory
    gpu = probe_apple_unified_memory(timeout=1.0, runner=lambda argv, timeout: None)
    ctx.check("None, never a guess", gpu is None)


@test
def test_probe_local_gpu_memory_apple_fraction_is_second_attempt_on_darwin(ctx: Ctx):
    """`_probe_apple` (the discrete-VRAM system_profiler scan) returning
    None is what lets `probe_apple_unified_memory` even get tried --
    patch `sys.platform` to "darwin" for the duration of this one test
    only, restored in `finally` no matter what."""
    import sys as _sys
    from halo_harness.providers import ollama_hw
    old_platform = _sys.platform
    _sys.platform = "darwin"
    try:
        total = 16 * 1024**3

        def runner(argv, timeout):
            if argv[0] == "system_profiler":
                return None  # no discrete VRAM line (the expected Apple-Silicon case)
            if argv[-1] == "hw.memsize":
                return f"{total}\n"
            return ""
        gpu = ollama_hw.probe_local_gpu_memory(timeout=1.0, runner=runner)
        ctx.check("falls through to the unified-memory estimate", gpu is not None and gpu.vendor == "apple")
        ctx.check("estimated (fraction-based)", gpu.estimated is True)
    finally:
        _sys.platform = old_platform


# ---- optional ssh GPU transport (never a real ssh binary) -----------------

@test
def test_run_via_ssh_builds_batchmode_argv_never_prompts(ctx: Ctx):
    """`run_via_ssh` itself shells out via `subprocess.run` -- there is no
    `runner` seam INSIDE `run_via_ssh` (it IS the runner); this test
    monkeypatches `subprocess.run` for the duration only, restored in
    `finally`, and never actually invokes a real `ssh` binary."""
    import subprocess as _subprocess
    from halo_harness.providers import ollama_hw
    captured = {}
    real_run = _subprocess.run

    class _FakeResult:
        returncode = 0
        stdout = "100, 10, 90, Remote GPU\n"

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _FakeResult()
    _subprocess.run = fake_run
    try:
        runner = ollama_hw.run_via_ssh("user@host", timeout=3.0)
        out = runner(["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free,name",
                       "--format=csv,noheader,nounits"], 3.0)
        ctx.check(f"stdout passed through, got {out!r}", out == "100, 10, 90, Remote GPU\n")
        argv = captured["argv"]
        ctx.check(f"ssh invoked, got {argv}", argv[0] == "ssh")
        ctx.check("BatchMode=yes (never an interactive prompt)", "BatchMode=yes" in argv)
        ctx.check("a ConnectTimeout option is set", any(a.startswith("ConnectTimeout=") for a in argv))
        ctx.check("the target host/user is on the command line", "user@host" in argv)
        ctx.check("the remote command is shell-quoted and present",
                  any("nvidia-smi" in a for a in argv))
    finally:
        _subprocess.run = real_run


@test
def test_run_via_ssh_degrades_to_none_on_failure(ctx: Ctx):
    import subprocess as _subprocess
    from halo_harness.providers import ollama_hw
    real_run = _subprocess.run

    def raising_run(argv, **kwargs):
        raise OSError("ssh not found")
    _subprocess.run = raising_run
    try:
        runner = ollama_hw.run_via_ssh("user@host", timeout=1.0)
        ctx.check("None on a subprocess failure, never raises", runner(["nvidia-smi"], 1.0) is None)
    finally:
        _subprocess.run = real_run


@test
def test_probe_remote_gpu_memory_uses_injected_runner_tries_nvidia_first(ctx: Ctx):
    """The brief's own test seam: `probe_remote_gpu_memory`'s `runner`
    kwarg REPLACES the ssh transport entirely -- this is what every
    pinning test (and `ollama_hw.estimate_fit_for_host`'s own ssh branch)
    actually uses, never a real `ssh` process."""
    from halo_harness.providers.ollama_hw import probe_remote_gpu_memory
    csv = "100, 10, 90, Remote GPU\n"
    gpu = probe_remote_gpu_memory("user@host", timeout=1.0, runner=lambda argv, timeout: csv)
    ctx.check("nvidia branch answers first", gpu is not None and gpu.vendor == "nvidia")
    ctx.check(f"free bytes, got {gpu.free_bytes}", gpu.free_bytes == 90 * 1024 * 1024)


@test
def test_probe_remote_gpu_memory_falls_through_every_branch_to_none(ctx: Ctx):
    from halo_harness.providers.ollama_hw import probe_remote_gpu_memory
    gpu = probe_remote_gpu_memory("user@host", timeout=1.0, runner=lambda argv, timeout: None)
    ctx.check("None when every vendor branch fails, never a guess", gpu is None)


@test
def test_get_ssh_gpu_memory_cached_per_host_string(ctx: Ctx):
    from halo_harness.providers import ollama_hw
    _reset_caches()
    try:
        calls = {"n": 0}

        def runner(argv, timeout):
            calls["n"] += 1
            return "100, 10, 90, X\n"
        first = ollama_hw.get_ssh_gpu_memory("user@host-a", runner=runner)
        second = ollama_hw.get_ssh_gpu_memory("user@host-a", runner=runner)
        ctx.check("cached within the TTL -- same object", first is second)
        ctx.check(f"the ssh transport ran exactly once, got {calls['n']}", calls["n"] == 1)
        third = ollama_hw.get_ssh_gpu_memory("user@host-b", runner=runner)
        ctx.check(f"a DIFFERENT host string gets its own probe, got {calls['n']}", calls["n"] == 2)
        ctx.check("different host, different reading object", third is not first)
    finally:
        _reset_caches()


# ---- last_known_offload (brief item 7, zero new network calls) ------------

@test
def test_last_known_offload_tracks_size_vs_size_vram(ctx: Ctx):
    from halo_harness.providers.ollama_hw import _record_last_known_offload, last_known_offload
    _record_last_known_offload("http://h", "m", {"size": 100, "size_vram": 60})
    ctx.check("partially offloaded -> True", last_known_offload("http://h", "m") is True)
    _record_last_known_offload("http://h", "m", {"size": 100, "size_vram": 100})
    ctx.check("fully resident -> False", last_known_offload("http://h", "m") is False)


@test
def test_last_known_offload_clears_when_not_loaded(ctx: Ctx):
    from halo_harness.providers.ollama_hw import _record_last_known_offload, last_known_offload
    _record_last_known_offload("http://h2", "m2", {"size": 100, "size_vram": 60})
    ctx.check("set first", last_known_offload("http://h2", "m2") is True)
    _record_last_known_offload("http://h2", "m2", None)  # no longer loaded
    ctx.check("cleared, not 'False' -- 'not loaded' != 'not offloaded'",
              last_known_offload("http://h2", "m2") is None)


@test
def test_last_known_offload_unknown_pair_is_none(ctx: Ctx):
    from halo_harness.providers.ollama_hw import last_known_offload
    ctx.check("never probed -> None", last_known_offload("http://never-seen", "m") is None)


# ---- OLLAMA_GPU_OVERHEAD (surfaced from the environment, no new config key) --

@test
def test_ollama_gpu_overhead_bytes_reads_env_and_defaults_to_zero(ctx: Ctx):
    from halo_harness.providers.ollama_hw import _ollama_gpu_overhead_bytes
    ctx.check("unset -> 0", _ollama_gpu_overhead_bytes({}) == 0)
    ctx.check("a real value", _ollama_gpu_overhead_bytes({"OLLAMA_GPU_OVERHEAD": "536870912"}) == 536870912)
    ctx.check("garbage -> 0, never raises", _ollama_gpu_overhead_bytes({"OLLAMA_GPU_OVERHEAD": "not-a-number"}) == 0)
    ctx.check("negative -> 0 (never a negative overhead)",
              _ollama_gpu_overhead_bytes({"OLLAMA_GPU_OVERHEAD": "-5"}) == 0)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

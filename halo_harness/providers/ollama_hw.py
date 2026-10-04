"""halo_harness.providers.ollama_hw -- Halo 2.0.3 round 3 (brief item 1/2):
per-host hardware reads for the `ol:` dialect, and the orchestration that
turns a (host, model, catalog) triple into the numbers `agent/loop.py`'s
`_build_ollama_body_for_ref`/`_sync_ollama_tools_cap` need. The pure
arithmetic (KV bytes/token, the fit-estimate formula, tools_max classes)
lives in `providers.ollama_fit` -- this module owns the I/O: shelling out
to an OS GPU-memory tool for a LOCAL host, and reading `/api/ps` for a
REMOTE one (research doc section 3: "GPU vendor/memory detection is OS
tooling, not an Ollama API" -- there is no HTTP endpoint to ask a remote
box how much VRAM it has, so a remote host's fit estimate can only ever
come from what it already reports about a model it has already loaded).

NVIDIA flags verified LIVE on this round's build host (an NVIDIA card):
`nvidia-smi --query-gpu=memory.total,memory.used,memory.free,name
--format=csv,noheader,nounits` prints one CSV line of plain MiB integers
plus the GPU name, no header row, no "MiB" suffix to strip -- see
`docs/harness/LOCAL-MODELS-RESEARCH.md` section 3's corrected note. The
AMD (`rocm-smi`/sysfs) and Apple (`system_profiler`) branches below stay
UNCONFIRMED at the exact-output-format level -- no such hardware was
available to verify this round; each degrades to `None` (never a guess)
on anything unexpected.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from halo_harness.providers.ollama_fit import (
    estimate_catalog_prompt_tokens, fit_estimate, kv_bytes_per_token,
    remote_loaded_context_as_fit_estimate, resolve_ollama_tools_max,
)

log = logging.getLogger("bridge")

_GPU_PROBE_TIMEOUT_S = 3.0
_HW_CACHE_TTL_S = 60.0  # brief item 2: "a turn never shells out more than once per minute"


@dataclass(frozen=True)
class GpuMemory:
    vendor: str  # "nvidia" | "amd" | "apple" | "unknown"
    name: Optional[str]
    total_bytes: Optional[int]
    free_bytes: Optional[int]


def _run_tool(argv: list, *, timeout: float, runner=None) -> Optional[str]:
    """stdout text on a clean (`returncode == 0`) run, `None` on anything
    else (tool missing, non-zero exit, timeout) -- `runner`, when given,
    REPLACES `subprocess.run` entirely (the test seam the brief asks for:
    a pinning test injects canned stdout per vendor branch, never a real
    OS tool)."""
    if runner is not None:
        return runner(argv, timeout)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return result.stdout if result.returncode == 0 else None


def _probe_nvidia(*, timeout: float, runner=None) -> Optional[GpuMemory]:
    out = _run_tool(["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free,name",
                      "--format=csv,noheader,nounits"], timeout=timeout, runner=runner)
    if not out or not out.strip():
        return None
    # Multi-GPU boxes print one line per card -- the first (index 0) is
    # reported; good enough for the common single-card host, short of the
    # full picture on a multi-GPU one (documented here, not hidden).
    parts = [p.strip() for p in out.strip().splitlines()[0].split(",")]
    if len(parts) < 3:
        return None
    try:
        total_mib, _used_mib, free_mib = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    name = parts[3] if len(parts) > 3 and parts[3] else None
    return GpuMemory(vendor="nvidia", name=name, total_bytes=total_mib * 1024 * 1024,
                      free_bytes=free_mib * 1024 * 1024)


def _probe_amd_rocm_smi(*, timeout: float, runner=None) -> Optional[GpuMemory]:
    """UNCONFIRMED (research doc section 3): no AMD/ROCm hardware
    available to verify this round -- `rocm-smi --showmeminfo vram`'s
    exact output format is carried over from the research doc's own
    naming, not live-verified. Scans for "Total Memory (B)"/"Total Used
    Memory (B)"-shaped lines; `None` on anything else, never a guess."""
    out = _run_tool(["rocm-smi", "--showmeminfo", "vram"], timeout=timeout, runner=runner)
    if not out:
        return None
    total_m = re.search(r"Total Memory \(B\)\s*:\s*(\d+)", out, re.IGNORECASE)
    used_m = re.search(r"Total Used Memory \(B\)\s*:\s*(\d+)", out, re.IGNORECASE)
    if not total_m:
        return None
    total = int(total_m.group(1))
    free = (total - int(used_m.group(1))) if used_m else None
    return GpuMemory(vendor="amd", name=None, total_bytes=total, free_bytes=free)


def _probe_amd_sysfs(*, runner=None) -> Optional[GpuMemory]:
    """UNCONFIRMED (research doc section 3): sysfs only ever gives the
    TOTAL (`mem_info_vram_total`) -- no documented sibling file for FREE/
    USED was found, so `free_bytes` stays `None` even on success. Never
    invoked under a test `runner` (there is no subprocess to replace
    here); real callers only reach this on Linux."""
    if runner is not None or sys.platform != "linux":
        return None
    import glob
    from pathlib import Path
    for path in sorted(glob.glob("/sys/class/drm/card*/device/mem_info_vram_total")):
        try:
            total = int(Path(path).read_text().strip())
        except (OSError, ValueError):
            continue
        return GpuMemory(vendor="amd", name=None, total_bytes=total, free_bytes=None)
    return None


def _probe_apple(*, timeout: float, runner=None) -> Optional[GpuMemory]:
    """UNCONFIRMED (research doc section 3): no macOS host available to
    verify this round. `system_profiler SPDisplaysDataType` prints human-
    readable text, not a machine-readable format -- this scans for a
    "VRAM (Total): <n> GB"-shaped line; Apple Silicon's unified memory is
    typically NOT itemized this way at all, so `None` here is the
    EXPECTED result on most current Macs, not a bug."""
    out = _run_tool(["system_profiler", "SPDisplaysDataType"], timeout=timeout, runner=runner)
    if not out:
        return None
    m = re.search(r"VRAM[^:]*:\s*([\d.]+)\s*(GB|MB)", out, re.IGNORECASE)
    if not m:
        return None
    mult = 1024 ** 3 if m.group(2).upper() == "GB" else 1024 ** 2
    return GpuMemory(vendor="apple", name=None, total_bytes=int(float(m.group(1)) * mult), free_bytes=None)


def probe_local_gpu_memory(*, timeout: float = _GPU_PROBE_TIMEOUT_S, runner=None) -> Optional[GpuMemory]:
    """Best-effort local OS-level GPU memory read, trying NVIDIA first
    (cross-platform: Windows and Linux both use `nvidia-smi`), then the
    platform fallback -- degrades to `None` on ANY failure; callers must
    treat `None` as "unknown", never as "no GPU"/zero."""
    gpu = _probe_nvidia(timeout=timeout, runner=runner)
    if gpu is not None:
        return gpu
    if sys.platform == "darwin":
        return _probe_apple(timeout=timeout, runner=runner)
    gpu = _probe_amd_rocm_smi(timeout=timeout, runner=runner)
    return gpu if gpu is not None else _probe_amd_sysfs(runner=runner)


_HW_LOCK = threading.Lock()
_HW_CACHE: "Optional[tuple]" = None  # (monotonic_ts, GpuMemory_or_None)


def get_local_gpu_memory(*, ttl_s: float = _HW_CACHE_TTL_S, force: bool = False, runner=None) -> Optional[GpuMemory]:
    """Cached `probe_local_gpu_memory` -- one process-wide slot (there is
    only ever one local machine, regardless of how many `ollama.hosts`
    entries point at it), refreshed at most once per `ttl_s` (brief item
    2's "never shells out more than once per minute"). Honours
    `BRIDGE_TEST_NO_BACKGROUND_NET` -- a real shell-out never happens
    under that flag -- UNLESS `runner` is given: an explicit test seam
    already fully controls the subprocess boundary, so it bypasses the
    flag rather than making every pinning test export it too."""
    global _HW_CACHE
    if runner is None:
        from halo_harness.config.paths import background_net_disabled
        if background_net_disabled():
            return None
    with _HW_LOCK:
        cached = _HW_CACHE
    now = time.monotonic()
    if cached is not None and not force and (now - cached[0]) < ttl_s:
        return cached[1]
    fresh = probe_local_gpu_memory(runner=runner)
    with _HW_LOCK:
        _HW_CACHE = (now, fresh)
    return fresh


def reset_local_gpu_cache() -> None:
    """Test seam: force the next `get_local_gpu_memory()` call to re-probe."""
    global _HW_CACHE
    with _HW_LOCK:
        _HW_CACHE = None


def is_local_host(host) -> bool:
    """`host.url`'s hostname is loopback -- the common-case heuristic
    (brief item 1: local hosts get the OS probe, remote hosts infer from
    `/api/ps` alone). A LAN host configured by an IP that happens to BE
    this machine is misclassified as remote; documented limitation, not a
    silent wrong answer (it just falls back to the remote, `/api/ps`-only
    picture, never a crash)."""
    hostname = (urllib.parse.urlparse(host.url).hostname or "").lower()
    return hostname in ("127.0.0.1", "localhost", "::1")


def catalog_row(catalog: Optional[dict], model: str) -> Optional[dict]:
    """The `/api/tags`+`/api/show`-merged row for `model` in an already-
    fetched `catalog` (`providers.ollama.get_catalog`'s own shape) --
    shared by this module's `estimate_fit_for_host` and `providers.
    ollama_panel.analyze_host`, so there is exactly one lookup to keep in
    sync with that shape."""
    for row in (catalog or {}).get("models") or []:
        if isinstance(row, dict) and (row.get("model") == model or row.get("name") == model):
            return row
    return None


def estimate_fit_for_host(host, model: str, catalog: Optional[dict], *, runner=None):
    """Brief item 2's end-to-end fit estimate for `providers.ollama.
    compute_num_ctx`'s `fit_estimate` argument -- returns a positive int,
    `None` (unknown), or `providers.ollama_fit.WEIGHTS_DO_NOT_FIT` (known
    not to fit; see that sentinel's own docstring).

    Round 3 fix pass (live-run finding): `/api/ps` is read FIRST now, on
    BOTH local and remote hosts -- a local host whose model is ALREADY
    LOADED trusts that loaded `context_length` exactly like the remote
    path always has, and never re-runs the GPU-memory arithmetic for it.
    Before this fix, a loaded model's `num_ctx` was recomputed from
    scratch every turn; two concurrent print-mode processes measuring
    free VRAM a few hundred MB apart was enough to compute two DIFFERENT
    `num_ctx` values for the same already-loaded model, and Ollama
    reloaded it (with partial CPU offload) just to change its context
    size. A LOCAL host whose model is NOT loaded uses the OS GPU-memory
    probe (cached, see `get_local_gpu_memory`) against this model's KV
    formula and its `/api/tags` `size` (the on-disk quantized weight
    size, the same bytes Ollama loads into VRAM) -- `free_memory_bytes`
    is augmented with every OTHER currently-loaded model's own
    `size_vram` first, since Ollama can evict any of them to make room
    for this one (so that VRAM is reclaimable headroom, not actually
    unavailable). A REMOTE host never gets an OS-level read at all
    (research doc section 3) -- its own `/api/ps` entry (loaded or not)
    is the only signal available, unchanged from round 3's own original
    design."""
    row = catalog_row(catalog, model)
    if row is None:
        return None
    from halo_harness.providers.ollama import fetch_ps
    ps = fetch_ps(host) or {}
    ps_models = [e for e in (ps.get("models") or []) if isinstance(e, dict)]
    loaded_entry = None
    for entry in ps_models:
        if entry.get("model") == model or entry.get("name") == model:
            loaded_entry = entry
            break
    if not is_local_host(host):
        return remote_loaded_context_as_fit_estimate(loaded_entry)
    if loaded_entry is not None:
        return remote_loaded_context_as_fit_estimate(loaded_entry)
    gpu = get_local_gpu_memory(runner=runner)
    if gpu is None or gpu.free_bytes is None:
        return None
    kv = kv_bytes_per_token(row.get("model_info") or {})
    weight_bytes = row.get("size")
    if kv is None or not isinstance(weight_bytes, int):
        return None
    reclaimable = sum(e.get("size_vram") for e in ps_models if isinstance(e.get("size_vram"), int))
    return fit_estimate(kv_bytes_per_token=kv, free_memory_bytes=gpu.free_bytes + reclaimable,
                         resident_weight_bytes=weight_bytes)


@dataclass(frozen=True)
class OllamaContextDecision:
    trained_context: Optional[int]
    fit_estimate: object  # Optional[int] | providers.ollama_fit.WEIGHTS_DO_NOT_FIT -- see that sentinel's docstring
    num_ctx: int
    tools_max: int
    catalog_prompt_tokens: int


def resolve_context_decision(model_ref, env=None, *, hw_runner=None) -> OllamaContextDecision:
    """ONE per-ref entry point for everything round 3 needs to know about
    an `ol:` ref right now: resolves its host, reads the (short-TTL
    cached) catalog for trained context, reads the fit estimate, and runs
    the SAME `compute_num_ctx`/`resolve_ollama_tools_max` arithmetic a
    real request through `_build_ollama_body_for_ref` already does.
    `agent/loop.py`'s `_sync_ollama_tools_cap` and `headless.py`'s
    session-build cap decision both call this instead of repeating the
    host/catalog dance inline. Never raises -- any failure degrades the
    relevant field to `None`/the safe default, exactly like a real
    request's own best-effort catalog read already does."""
    from halo_harness.providers.ollama import compute_num_ctx, get_catalog, resolve_ollama_host, trained_context_for
    host = resolve_ollama_host(getattr(model_ref, "host", None), env)
    trained_context, fit, host_max_ctx = None, None, None
    if host is not None:
        host_max_ctx = host.max_ctx
        catalog = None
        try:
            catalog = get_catalog(host)
            trained_context = trained_context_for(catalog, model_ref.model)
        except Exception:
            log.debug("ollama_hw: catalog read failed for %s", model_ref.model, exc_info=True)
        try:
            fit = estimate_fit_for_host(host, model_ref.model, catalog, runner=hw_runner) if catalog else None
        except Exception:
            log.debug("ollama_hw: fit estimate failed for %s", model_ref.model, exc_info=True)
    num_ctx = compute_num_ctx(trained_context, host_max_ctx, fit)
    tools_max = resolve_ollama_tools_max(num_ctx)
    return OllamaContextDecision(trained_context=trained_context, fit_estimate=fit, num_ctx=num_ctx,
                                  tools_max=tools_max, catalog_prompt_tokens=estimate_catalog_prompt_tokens(tools_max))

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
    estimate_catalog_prompt_tokens, fit_estimate, kv_bytes_per_elem_for, kv_bytes_per_token,
    multi_gpu_fit_estimate, remote_loaded_context_as_fit_estimate, resolve_ollama_tools_max,
)

log = logging.getLogger("bridge")

_GPU_PROBE_TIMEOUT_S = 3.0
_HW_CACHE_TTL_S = 60.0  # brief item 2: "a turn never shells out more than once per minute"

# Round 5b (docs/harness/GPU-RESEARCH.md "Apple Silicon memory-share
# rule"): no fetched Apple/mlx-lm source gives an exact default fraction
# of unified RAM the GPU may use -- only "about two-thirds to three-
# quarters", UNCONFIRMED at the exact figure. The midpoint is a wizard/
# panel FIRST GUESS, explicitly labelled an estimate (`GpuMemory.estimated`
# below); `halo ollama calibrate` is the ground truth once a real model
# has actually been loaded, same as the research doc's own framing.
APPLE_GPU_SHARE_FRACTION_DEFAULT = 0.7


@dataclass(frozen=True)
class GpuMemory:
    vendor: str  # "nvidia" | "amd" | "apple" | "unknown"
    name: Optional[str]
    total_bytes: Optional[int]
    free_bytes: Optional[int]
    # Round 5b: True only for the Apple unified-memory FRACTION-based
    # reading (probe_apple_unified_memory, when `iogpu.wired_limit_mb`
    # isn't set) -- a discrete-GPU reading (NVIDIA/AMD/an explicit Apple
    # wired_limit) is a real measurement, never "estimated". Panels label
    # this reading as an estimate per the research doc's own instruction.
    estimated: bool = False


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


def _probe_nvidia(*, timeout: float, runner=None, probe_all: bool = False):
    """Round 5b (GPU-RESEARCH.md section 1): `memory.free` already
    EXCLUDES `memory.reserved` ("total memory reserved by the NVIDIA
    driver and firmware", live-confirmed: `total = reserved + used +
    free`) -- this function reads `memory.free` directly rather than
    computing `total - used` by hand, so it never double-counts the
    reserved slice as available. `probe_all=False` (default, unchanged
    from before round 5b): a single `Optional[GpuMemory]`, the FIRST card
    only. `probe_all=True` (round 5b, multi-GPU): `list[GpuMemory]`, one
    per line `nvidia-smi` printed (every card), `[]` (never `None`) when
    the tool produced no usable line at all -- a distinct empty-list
    return so a caller can tell "ran, zero cards" from "didn't run"."""
    out = _run_tool(["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free,name",
                      "--format=csv,noheader,nounits"], timeout=timeout, runner=runner)
    if not out or not out.strip():
        return [] if probe_all else None
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    if not probe_all:
        lines = lines[:1]
    cards = []
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total_mib, _used_mib, free_mib = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        name = parts[3] if len(parts) > 3 and parts[3] else None
        cards.append(GpuMemory(vendor="nvidia", name=name, total_bytes=total_mib * 1024 * 1024,
                                free_bytes=free_mib * 1024 * 1024))
    if probe_all:
        return cards
    return cards[0] if cards else None


def probe_nvidia_cuda_version(*, timeout: float = _GPU_PROBE_TIMEOUT_S, runner=None) -> Optional[str]:
    """Halo 2.0.3 round 5c (GPU-RESEARCH.md section 4, "driver-version
    rule"): `nvidia-smi -q`'s own "CUDA Version" line (GitHub's own docs
    call the identical field "CUDA UMD Version") -- the highest CUDA
    toolkit the INSTALLED DRIVER supports, read directly rather than
    maintained as a hand-written driver/toolkit lookup table. `providers.
    local_runtime_fetch`'s asset picker uses this to choose a llama.cpp
    CUDA build: the highest `cuda-<VER>` release asset whose `<VER>` is
    `<=` this reported number. `None` on anything unexpected (no
    `nvidia-smi`, no such line in the output) -- never a guess."""
    out = _run_tool(["nvidia-smi", "-q"], timeout=timeout, runner=runner)
    if not out:
        return None
    m = re.search(r"CUDA Version\s*:\s*([\d.]+)", out)
    return m.group(1) if m else None


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


def probe_apple_unified_memory(*, timeout: float = _GPU_PROBE_TIMEOUT_S, runner=None) -> Optional[GpuMemory]:
    """Round 5b (GPU-RESEARCH.md section 1/"Apple Silicon memory-share
    rule"): Apple Silicon has no discrete VRAM to query (`_probe_apple`'s
    own `system_profiler` scan correctly returns `None` there) -- this is
    the SECOND attempt `probe_local_gpu_memory`'s darwin branch makes,
    never a replacement for `_probe_apple` (a future Mac that DOES report
    a real `system_profiler` VRAM line should still win over this
    fraction-based estimate). Reads `sysctl -n hw.memsize` (total RAM,
    bytes -- UNCONFIRMED against an Apple primary source, a long-standing
    stable macOS CLI surface per the research doc) and `sysctl -n
    iogpu.wired_limit_mb` (confirmed from mlx-lm's own README: "wires the
    memory occupied by the model and cache"; 0/unset/unparseable means
    "not configured"). `free_bytes` is the wired limit (exact, bytes) when
    set and positive, else `hw.memsize * APPLE_GPU_SHARE_FRACTION_DEFAULT`
    (`estimated=True` in that branch only) -- `None` when `hw.memsize`
    itself can't be read (two separate shell-outs; a test `runner` must
    branch on `argv[-1]`, the sysctl key name, to answer both)."""
    total_out = _run_tool(["sysctl", "-n", "hw.memsize"], timeout=timeout, runner=runner)
    if not total_out or not total_out.strip():
        return None
    try:
        total = int(total_out.strip())
    except ValueError:
        return None
    if total <= 0:
        return None
    wired_out = _run_tool(["sysctl", "-n", "iogpu.wired_limit_mb"], timeout=timeout, runner=runner)
    wired_mb = None
    if wired_out and wired_out.strip():
        try:
            wired_mb = int(wired_out.strip())
        except ValueError:
            wired_mb = None
    if wired_mb and wired_mb > 0:
        return GpuMemory(vendor="apple", name=None, total_bytes=total,
                          free_bytes=wired_mb * 1024 * 1024, estimated=False)
    usable = int(total * APPLE_GPU_SHARE_FRACTION_DEFAULT)
    return GpuMemory(vendor="apple", name=None, total_bytes=total, free_bytes=usable, estimated=True)


def probe_local_gpu_memory(*, timeout: float = _GPU_PROBE_TIMEOUT_S, runner=None) -> Optional[GpuMemory]:
    """Best-effort local OS-level GPU memory read, trying NVIDIA first
    (cross-platform: Windows and Linux both use `nvidia-smi`), then the
    platform fallback -- degrades to `None` on ANY failure; callers must
    treat `None` as "unknown", never as "no GPU"/zero."""
    gpu = _probe_nvidia(timeout=timeout, runner=runner)
    if gpu is not None:
        return gpu
    if sys.platform == "darwin":
        gpu = _probe_apple(timeout=timeout, runner=runner)
        # Round 5b: a discrete-style VRAM line (rare on Apple Silicon, the
        # expected outcome on current Macs per _probe_apple's own
        # docstring) still wins over the unified-memory fraction estimate.
        return gpu if gpu is not None else probe_apple_unified_memory(timeout=timeout, runner=runner)
    gpu = _probe_amd_rocm_smi(timeout=timeout, runner=runner)
    return gpu if gpu is not None else _probe_amd_sysfs(runner=runner)


def run_via_ssh(user_host: str, *, timeout: float = _GPU_PROBE_TIMEOUT_S):
    """Round 5b (GPU-RESEARCH.md section 3, `ollama.hosts[].ssh`): a
    `runner(argv, call_timeout)` callable -- the SAME test seam every
    `_probe_*` function in this module already accepts -- that runs
    `argv` on `user_host` over ssh instead of a local subprocess:
    `ssh -o BatchMode=yes -o ConnectTimeout=<n> user@host '<argv, shell-
    quoted>'`. `BatchMode=yes` refuses a password/host-key prompt outright
    rather than hanging (read-only, never interactive); never required,
    never prompted for (`ollama.hosts[].ssh` is an opt-in config key --
    see docs/MODELS.md/docs/CONFIG.md). Degrades to `None` on anything
    (ssh missing, refused connection, non-zero exit, timeout) exactly like
    `_run_tool`'s own local path -- plug it straight into any existing
    probe (`_probe_nvidia(runner=run_via_ssh("user@host"))`) with no
    changes to that probe itself."""
    import shlex

    def _runner(argv: list, call_timeout: float) -> Optional[str]:
        remote_cmd = " ".join(shlex.quote(str(a)) for a in argv)
        ssh_argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(timeout))}",
                    user_host, remote_cmd]
        try:
            result = subprocess.run(ssh_argv, capture_output=True, text=True, timeout=call_timeout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        return result.stdout if result.returncode == 0 else None
    return _runner


def probe_remote_gpu_memory(user_host: str, *, timeout: float = _GPU_PROBE_TIMEOUT_S,
                             runner=None) -> Optional[GpuMemory]:
    """Round 5b's OPTIONAL ssh GPU read for a remote `ollama.hosts[]`
    entry (`ssh: "user@host"`): the SAME vendor probes section 1 already
    runs locally, over `run_via_ssh` instead -- never the sysfs branch
    (`_probe_amd_sysfs` reads a LOCAL file path directly, not an argv, so
    it has no ssh-shaped equivalent; a documented gap, not a silent one).
    `runner`, when given, REPLACES the ssh transport entirely (pinning
    tests inject canned stdout per vendor branch, never a real ssh binary
    or network). We don't know the remote OS in advance, so every branch
    is tried in turn, same "degrade to None, never guess" discipline as
    `probe_local_gpu_memory`."""
    effective_runner = runner if runner is not None else run_via_ssh(user_host, timeout=timeout)
    gpu = _probe_nvidia(timeout=timeout, runner=effective_runner)
    if gpu is not None:
        return gpu
    gpu = _probe_amd_rocm_smi(timeout=timeout, runner=effective_runner)
    if gpu is not None:
        return gpu
    return probe_apple_unified_memory(timeout=timeout, runner=effective_runner)


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


_HW_CACHE_MULTI: "Optional[tuple]" = None  # (monotonic_ts, list[GpuMemory])


def get_local_gpu_memories(*, ttl_s: float = _HW_CACHE_TTL_S, force: bool = False, runner=None) -> list:
    """Round 5b: the list-returning sibling of `get_local_gpu_memory`, for
    `estimate_fit_for_host`'s multi-GPU branch. Tries the NVIDIA
    `probe_all=True` probe first; on anything else (no `nvidia-smi`, a
    single-card box, AMD/Apple) falls back to a 0-or-1-element list built
    from `get_local_gpu_memory` itself -- so a single-GPU host's result is
    sourced from the EXACT SAME cached call `get_local_gpu_memory` already
    uses, never a second, independently-cached single-card reading that
    could disagree with it. Shares `_HW_LOCK`/the `BRIDGE_TEST_NO_
    BACKGROUND_NET` guard with `get_local_gpu_memory`; `[]` (never `None`)
    on total failure, matching `_probe_nvidia(probe_all=True)`'s own
    empty-list contract."""
    global _HW_CACHE_MULTI
    if runner is None:
        from halo_harness.config.paths import background_net_disabled
        if background_net_disabled():
            return []
    with _HW_LOCK:
        cached = _HW_CACHE_MULTI
    now = time.monotonic()
    if cached is not None and not force and (now - cached[0]) < ttl_s:
        return cached[1]
    cards = _probe_nvidia(timeout=_GPU_PROBE_TIMEOUT_S, runner=runner, probe_all=True)
    if not cards:
        single = get_local_gpu_memory(ttl_s=ttl_s, force=force, runner=runner)
        cards = [single] if single is not None else []
    with _HW_LOCK:
        _HW_CACHE_MULTI = (now, cards)
    return cards


def reset_local_gpu_cache() -> None:
    """Test seam: force the next `get_local_gpu_memory()`/`get_local_gpu_
    memories()` call to re-probe."""
    global _HW_CACHE, _HW_CACHE_MULTI
    with _HW_LOCK:
        _HW_CACHE = None
        _HW_CACHE_MULTI = None


_SSH_HW_LOCK = threading.Lock()
_SSH_HW_CACHE: dict = {}  # user_host -> (monotonic_ts, GpuMemory_or_None)


def get_ssh_gpu_memory(user_host: str, *, ttl_s: float = _HW_CACHE_TTL_S, force: bool = False,
                        runner=None) -> Optional[GpuMemory]:
    """Cached `probe_remote_gpu_memory` -- keyed by `user_host` (several
    `ollama.hosts[].ssh` entries each get their own cache slot, unlike the
    single process-wide slot `get_local_gpu_memory` uses for "the one
    local machine"). Same TTL/`BRIDGE_TEST_NO_BACKGROUND_NET`/`runner`-
    bypass contract as `get_local_gpu_memory`."""
    if runner is None:
        from halo_harness.config.paths import background_net_disabled
        if background_net_disabled():
            return None
    with _SSH_HW_LOCK:
        cached = _SSH_HW_CACHE.get(user_host)
    now = time.monotonic()
    if cached is not None and not force and (now - cached[0]) < ttl_s:
        return cached[1]
    fresh = probe_remote_gpu_memory(user_host, runner=runner)
    with _SSH_HW_LOCK:
        _SSH_HW_CACHE[user_host] = (now, fresh)
    return fresh


def reset_ssh_gpu_cache() -> None:
    """Test seam: force the next `get_ssh_gpu_memory()` call to re-probe."""
    with _SSH_HW_LOCK:
        _SSH_HW_CACHE.clear()


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
    sync with that shape. FIX PASS: matched via `ollama_names_match` (an
    untagged `model` must still find its own `:latest`-qualified row)."""
    from halo_harness.providers.ollama import ollama_names_match
    for row in (catalog or {}).get("models") or []:
        if isinstance(row, dict) and (ollama_names_match(row.get("model"), model)
                                       or ollama_names_match(row.get("name"), model)):
            return row
    return None


_LAST_OFFLOAD_LOCK = threading.Lock()
_LAST_OFFLOAD_CACHE: dict = {}  # (host.url, model) -> bool | None


def _record_last_known_offload(host_url: str, model: str, ps_entry: Optional[dict]) -> None:
    """Round 5b (brief item 7's "offloaded" marker): a side effect of the
    SAME `/api/ps` read `estimate_fit_for_host` already makes every turn
    -- zero new network calls. `None` (cleared, not "false") when `model`
    isn't currently loaded at all, or its `size`/`size_vram` fields are
    missing -- "not loaded" is not the same fact as "loaded and fully in
    GPU", and the status bar/`halo ollama` should say neither when this
    is unknown rather than guess "not offloaded"."""
    key = (host_url, model)
    size = (ps_entry or {}).get("size") if isinstance(ps_entry, dict) else None
    size_vram = (ps_entry or {}).get("size_vram") if isinstance(ps_entry, dict) else None
    with _LAST_OFFLOAD_LOCK:
        if isinstance(size, int) and isinstance(size_vram, int) and size > 0:
            _LAST_OFFLOAD_CACHE[key] = size_vram < size
        else:
            _LAST_OFFLOAD_CACHE.pop(key, None)


def last_known_offload(host_url: str, model: str) -> Optional[bool]:
    """The most recent `_record_last_known_offload` reading for (host_url,
    model), or `None` when unknown (never probed yet this process, or the
    model wasn't loaded at its last check)."""
    with _LAST_OFFLOAD_LOCK:
        return _LAST_OFFLOAD_CACHE.get((host_url, model))


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
    unavailable). A REMOTE host never gets an OS-level read UNLESS its
    config sets `ssh:` (round 5b, optional) -- then the exact same
    `_fit_from_gpu_cards` arithmetic runs against an ssh-probed card list
    instead of a local one; with no `ssh` configured, its own `/api/ps`
    entry (loaded or not) is the only signal available, unchanged from
    round 3's own original design."""
    row = catalog_row(catalog, model)
    if row is None:
        return None
    from halo_harness.providers.ollama import fetch_ps, ollama_names_match
    ps = fetch_ps(host) or {}
    ps_models = [e for e in (ps.get("models") or []) if isinstance(e, dict)]
    loaded_entry = None
    for entry in ps_models:
        if ollama_names_match(entry.get("model"), model) or ollama_names_match(entry.get("name"), model):
            loaded_entry = entry
            break
    _record_last_known_offload(host.url, model, loaded_entry)
    if loaded_entry is not None:
        return remote_loaded_context_as_fit_estimate(loaded_entry)
    if not is_local_host(host):
        ssh_target = getattr(host, "ssh", None)
        if not ssh_target:
            return None
        cards = [get_ssh_gpu_memory(ssh_target, runner=runner)]
        cards = [c for c in cards if c is not None]
        return _fit_from_gpu_cards(host, row, ps_models, cards)
    cards = get_local_gpu_memories(runner=runner)
    return _fit_from_gpu_cards(host, row, ps_models, cards)


def _ollama_gpu_overhead_bytes(env=None) -> int:
    """GPU-RESEARCH.md section 2: `OLLAMA_GPU_OVERHEAD` (confirmed from
    `envconfig/config.go`'s own doc comment, "Set aside VRAM per GPU" --
    absent from BOTH docs pages, source-only) -- "surfaced... rather than
    inventing a new config key," read directly from the environment, the
    SAME uint64-byte-count the Ollama SERVER itself reads. Only ever
    meaningful for a LOCAL host (Halo's own process shares that host's
    environment only when it IS that host); a remote host's own server-
    side value is invisible to Halo regardless. `0` (no overhead) on
    anything unset/unparseable -- never a guess at a human-readable unit
    the research doc did not confirm this var accepts."""
    import os
    raw = (env if env is not None else os.environ).get("OLLAMA_GPU_OVERHEAD")
    if not raw:
        return 0
    try:
        value = int(str(raw).strip())
    except ValueError:
        return 0
    return value if value > 0 else 0


def _fit_from_gpu_cards(host, row: dict, ps_models: list, cards: list):
    """Shared by `estimate_fit_for_host`'s local and ssh-remote branches
    (round 5b): `host.kv_cache_type` (default f16) picks the KV bytes/
    element constant, `row`'s own `size`/`model_info` give the weight
    size and the per-token formula, and every OTHER loaded model's
    `size_vram` is reclaimable headroom exactly as before round 5b.
    `len(cards) > 1` uses `multi_gpu_fit_estimate` (SUM, not min); `len ==
    1` calls `fit_estimate` directly, the identical formula for one card.
    Reclaimable VRAM is added to the total once (which card it's nominally
    credited to doesn't matter -- the multi-card formula only ever sums
    the list)."""
    free_cards = [c.free_bytes for c in (cards or []) if c is not None and isinstance(c.free_bytes, int)]
    if not free_cards:
        return None
    bytes_per_elem = kv_bytes_per_elem_for(getattr(host, "kv_cache_type", None))
    kv = kv_bytes_per_token(row.get("model_info") or {}, bytes_per_elem=bytes_per_elem)
    weight_bytes = row.get("size")
    if kv is None or not isinstance(weight_bytes, int):
        return None
    reclaimable = sum(e.get("size_vram") for e in ps_models if isinstance(e.get("size_vram"), int))
    free_cards = list(free_cards)
    free_cards[0] = free_cards[0] + reclaimable
    if len(free_cards) > 1:
        # Only ever meaningful on a LOCAL host -- see _ollama_gpu_overhead_
        # bytes' own docstring for why a remote (even ssh-probed) host's
        # own OLLAMA_GPU_OVERHEAD is invisible to this process.
        overhead = _ollama_gpu_overhead_bytes() if is_local_host(host) else 0
        return multi_gpu_fit_estimate(kv_bytes_per_token=kv, free_bytes_per_card=free_cards,
                                       resident_weight_bytes=weight_bytes, overhead_per_card_bytes=overhead)
    return fit_estimate(kv_bytes_per_token=kv, free_memory_bytes=free_cards[0],
                         resident_weight_bytes=weight_bytes)


@dataclass(frozen=True)
class OllamaContextDecision:
    trained_context: Optional[int]
    fit_estimate: object  # Optional[int] | providers.ollama_fit.WEIGHTS_DO_NOT_FIT -- see that sentinel's docstring
    num_ctx: int
    tools_max: int
    catalog_prompt_tokens: int
    # Round 5b additions -- `learned_cap`/`remote` are the two new inputs
    # `providers.ollama.resolve_num_ctx_and_source` takes beyond round 3's
    # three; `source` is that SAME call's second return value (the one
    # short phrase naming which of those five inputs actually decided
    # `num_ctx`), carried on the dataclass so `/ollama`/`halo ollama`/the
    # status bar never have to re-derive it themselves.
    learned_cap: Optional[int] = None
    remote: bool = False
    source: str = "hard cap"
    # Round 5b (brief item 7): `last_known_offload`'s reading for this
    # (host, model) -- a side effect of the SAME `/api/ps` call
    # `estimate_fit_for_host` just made above, never a second network
    # call. `None` when unknown (never probed, or not currently loaded).
    offloaded: Optional[bool] = None


def fits_beside_main(host, *, main_model: str, candidate_model: str, catalog: Optional[dict],
                      hw_runner=None) -> Optional[bool]:
    """Halo 2.0.3 round 5b part 2 (brief item 3, "VRAM-aware role
    defaults"): would `candidate_model`'s own on-disk weight size
    (`catalog_row(...)["size"]`, the same bytes Ollama loads into VRAM --
    `estimate_fit_for_host`'s own docstring) fit ALONGSIDE `main_model`'s
    CURRENTLY RESIDENT weight footprint (`/api/ps`'s own `size_vram` --
    requires `main_model` to be loaded right now; a measured fact, never
    a guess) without exceeding this host's total GPU memory. Literally
    the brief's own formula: "weights of the candidate plus the main
    model's resident size exceed the host's memory". `None` (unknown,
    never a guess -- the caller's own house policy of "benefit of the
    doubt" then applies) whenever ANY input is missing: `main_model`
    isn't currently loaded on this host, `candidate_model` isn't in
    `catalog` (or has no `size`), or no GPU memory total is readable at
    all (local: `get_local_gpu_memories`; remote: only when `host.ssh` is
    configured, else always `None` -- same reachability rule every other
    GPU read in this module already follows). `True`/`False` only when
    every input was a real measurement."""
    from halo_harness.providers.ollama import fetch_ps, ollama_names_match
    ps = fetch_ps(host) or {}
    main_entry = None
    for entry in (ps.get("models") or []):
        if isinstance(entry, dict) and (ollama_names_match(entry.get("model"), main_model)
                                         or ollama_names_match(entry.get("name"), main_model)):
            main_entry = entry
            break
    if main_entry is None:
        return None
    main_resident = main_entry.get("size_vram")
    if not isinstance(main_resident, int) or isinstance(main_resident, bool) or main_resident <= 0:
        return None
    candidate_row = catalog_row(catalog, candidate_model)
    candidate_weight = candidate_row.get("size") if candidate_row else None
    if not isinstance(candidate_weight, int) or isinstance(candidate_weight, bool) or candidate_weight <= 0:
        return None
    if is_local_host(host):
        cards = get_local_gpu_memories(runner=hw_runner)
    elif getattr(host, "ssh", None):
        card = get_ssh_gpu_memory(host.ssh, runner=hw_runner)
        cards = [card] if card is not None else []
    else:
        return None
    total_bytes = [c.total_bytes for c in cards if c is not None and isinstance(c.total_bytes, int)]
    if not total_bytes:
        return None
    return (main_resident + candidate_weight) <= sum(total_bytes)


def resolve_context_decision(model_ref, env=None, *, hw_runner=None) -> OllamaContextDecision:
    """ONE per-ref entry point for everything round 3 (and, as of round
    5b, the learned-cap/remote-default precedence) needs to know about an
    `ol:` ref right now: resolves its host, reads the (short-TTL cached)
    catalog for trained context and digest, looks up a learned calibration
    cap for (host, model, digest), reads the live fit estimate, and runs
    the SAME `resolve_num_ctx_and_source`/`resolve_ollama_tools_max`
    arithmetic a real request through `_build_ollama_body_for_ref`
    already does. `agent/loop.py`'s `_sync_ollama_tools_cap` and
    `headless.py`'s session-build cap decision both call this instead of
    repeating the host/catalog dance inline. Never raises -- any failure
    degrades the relevant field to `None`/the safe default, exactly like a
    real request's own best-effort catalog read already does."""
    from halo_harness.providers.ollama import resolve_num_ctx_and_source, get_catalog, resolve_ollama_host, \
        trained_context_for
    host = resolve_ollama_host(getattr(model_ref, "host", None), env)
    trained_context, fit, host_max_ctx, learned_cap = None, None, None, None
    remote = False
    if host is not None:
        host_max_ctx = host.max_ctx
        remote = not is_local_host(host)
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
        try:
            # Round 5b: DIGEST only here (never a fresh `/api/version` probe
            # on every single turn just for this lookup -- too expensive on
            # the hot path); a model-version mismatch is still caught at the
            # coarser granularity of `halo ollama calibrate`/the auto-
            # calibrate trigger re-measuring and overwriting the entry, both
            # of which already pay for a version probe for their own sake.
            from halo_harness.providers.ollama_calibrate import lookup_learned_cap
            from halo_harness.config.paths import bridge_home
            digest = (catalog_row(catalog, model_ref.model) or {}).get("digest") if catalog else None
            learned_cap = lookup_learned_cap(bridge_home(), host_url=host.url, model=model_ref.model, digest=digest)
        except Exception:
            log.debug("ollama_hw: learned-cap lookup failed for %s", model_ref.model, exc_info=True)
    num_ctx, source = resolve_num_ctx_and_source(trained_context, host_max_ctx, fit,
                                                  learned_cap=learned_cap, remote=remote)
    tools_max = resolve_ollama_tools_max(num_ctx)
    offloaded = last_known_offload(host.url, model_ref.model) if host is not None else None
    return OllamaContextDecision(trained_context=trained_context, fit_estimate=fit, num_ctx=num_ctx,
                                  tools_max=tools_max, catalog_prompt_tokens=estimate_catalog_prompt_tokens(tools_max),
                                  learned_cap=learned_cap, remote=remote, source=source, offloaded=offloaded)

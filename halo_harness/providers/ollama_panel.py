"""halo_harness.providers.ollama_panel -- Halo 2.0.3 round 3 (brief item
1/4): the per-host "what's actually happening" snapshot `/ollama` (TUI),
`halo ollama` (CLI) and `halo doctor`'s Ollama section all render from --
ONE builder (`analyze_host`) and ONE text formatter (`format_host_
analysis`) so the three surfaces can never quietly disagree.

A loaded model's offload state (`size` vs `size_vram`, research doc
section 3) is always reported as one plain sentence -- never a block,
never gated/blocked on -- per house policy against describing a slow
path as an error. Partial CPU offload is reported, not fixed: Halo never
refuses to use a model that doesn't fully fit in VRAM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from halo_harness.providers.ollama import (
    fetch_ps, get_catalog, probe_version, resolve_num_ctx_and_source, trained_context_for,
)
from halo_harness.providers.ollama_fit import estimate_catalog_prompt_tokens, kv_bytes_per_token, resolve_ollama_tools_max
from halo_harness.providers.ollama_hw import (
    GpuMemory, catalog_row, estimate_fit_for_host, get_local_gpu_memories, get_local_gpu_memory,
    get_ssh_gpu_memory, is_local_host,
)

# brief item 1: "label its origin in the panel help text" -- shown verbatim
# by format_host_analysis below, not just left in a docstring/the research doc.
KV_FORMULA_ORIGIN_NOTE = ("KV cache bytes/token: standard GGML/llama.cpp accounting "
                          "(2 x layers x kv_heads x head_dim x bytes/elem), not independently "
                          "re-derived this round -- see docs/harness/LOCAL-MODELS-RESEARCH.md section 2.")


def human_bytes(n: Optional[int]) -> str:
    if not isinstance(n, int) or isinstance(n, bool):
        return "unknown"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def offload_sentence(name: str, size: Optional[int], size_vram: Optional[int]) -> str:
    """ONE plain sentence (brief item 1) -- never a block, never a
    warning/error -- describing whether `name`'s weights are fully in GPU
    memory or partly in system RAM (slower, not blocked)."""
    if not isinstance(size, int) or not isinstance(size_vram, int) or size <= 0:
        return f"{name}: offload unknown (host did not report size/size_vram)."
    if size_vram >= size:
        return f"{name} is fully loaded in GPU memory."
    pct = round(size_vram / size * 100)
    return (f"{name} is partially offloaded: {human_bytes(size_vram)} of {human_bytes(size)} "
            f"in GPU memory ({pct}%), the rest in system RAM (slower).")


@dataclass(frozen=True)
class LoadedModelAnalysis:
    name: str
    size_bytes: Optional[int]
    size_vram_bytes: Optional[int]
    offload_sentence: str
    trained_context: Optional[int]
    effective_context: Optional[int]  # /api/ps's own context_length -- ground truth, not a guess
    kv_bytes_per_token: Optional[float]
    tools_max: int
    catalog_prompt_tokens: int


@dataclass(frozen=True)
class ModelFitInfo:
    """Round 5b (brief item 3): one catalog model's "what fits" row --
    EVERY catalog model, not just currently-loaded ones (the whole point
    of calibrating ahead of time is knowing what WOULD fit before loading
    it). `what_fits` is `resolve_num_ctx_and_source`'s own `num_ctx` --
    always a real, positive int, the same number a real request for this
    model would compute right now; `source` is that call's one short
    phrase. `throughput`, when known, is the brief item 7 "last turn"
    reading (`ollama_calibrate.get_last_turn_throughput`)."""
    name: str
    learned_cap: Optional[int]
    what_fits: int
    source: str
    throughput: Optional[dict] = None


@dataclass(frozen=True)
class HostAnalysis:
    host_name: str
    host_url: str
    is_local: bool
    reachable: bool
    version: Optional[str]
    loaded: "tuple"  # tuple[LoadedModelAnalysis, ...]
    gpu: Optional[GpuMemory]
    # Round 5b: every detected local GPU card (len 0 or 1 on a single-card/
    # no-GPU host, matching `gpu` above; len > 1 on a real multi-GPU box).
    # `()` (never populated) for a remote host with no `ssh` configured.
    gpu_cards: "tuple" = ()
    catalog_fits: "tuple" = ()  # tuple[ModelFitInfo, ...], one per catalog model


def analyze_host(host, *, hw_runner=None, force: bool = False, state_dir=None) -> HostAnalysis:
    """One host's full picture: reachability + version (`probe_version`),
    loaded models (`/api/ps`, cross-referenced against the catalog's
    `model_info` for trained context and the KV formula), OS-level GPU
    memory (LOCAL hosts always; a REMOTE host only when its config sets
    `ssh:`, round 5b -- otherwise `gpu`/`gpu_cards` stay empty, research
    doc section 3's original "no OS tool to shell out to remotely" still
    holds), and (round 5b) a `catalog_fits` row -- learned cap, "what
    fits" now, and its one-phrase source -- for EVERY catalog model, not
    just currently-loaded ones."""
    version_info = probe_version(host)
    reachable = isinstance(version_info, dict)
    version = (version_info or {}).get("version") if isinstance(version_info, dict) else None
    catalog = get_catalog(host, force=force) if reachable else {"models": []}
    ps = fetch_ps(host) if reachable else None
    loaded = []
    for entry in ((ps or {}).get("models") or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("model") or entry.get("name") or "?"
        size = entry.get("size") if isinstance(entry.get("size"), int) else None
        size_vram = entry.get("size_vram") if isinstance(entry.get("size_vram"), int) else None
        row = catalog_row(catalog, name)
        trained = trained_context_for(catalog, name)
        kv = kv_bytes_per_token((row or {}).get("model_info") or {}) if row else None
        effective = entry.get("context_length") if isinstance(entry.get("context_length"), int) else None
        tools_max = resolve_ollama_tools_max(effective if effective is not None else trained)
        loaded.append(LoadedModelAnalysis(
            name=name, size_bytes=size, size_vram_bytes=size_vram,
            offload_sentence=offload_sentence(name, size, size_vram),
            trained_context=trained, effective_context=effective, kv_bytes_per_token=kv,
            tools_max=tools_max, catalog_prompt_tokens=estimate_catalog_prompt_tokens(tools_max),
        ))
    local = is_local_host(host)
    gpu_cards: tuple = ()
    if local:
        gpu_cards = tuple(get_local_gpu_memories(runner=hw_runner))
        gpu = gpu_cards[0] if gpu_cards else None
    elif getattr(host, "ssh", None):
        # Round 5b: the OPTIONAL ssh GPU read -- a remote host with
        # nothing configured keeps `gpu=None` exactly as before this
        # round; `ssh:` opts a specific host into a real reading.
        ssh_gpu = get_ssh_gpu_memory(host.ssh, runner=hw_runner)
        gpu_cards = (ssh_gpu,) if ssh_gpu is not None else ()
        gpu = ssh_gpu
    else:
        gpu = None
    if state_dir is None:
        from halo_harness.config.paths import bridge_home
        state_dir = bridge_home()
    catalog_fits = tuple(_model_fit_info(host, catalog, row, state_dir=state_dir, remote=not local,
                                          hw_runner=hw_runner)
                         for row in (catalog.get("models") or []) if isinstance(row, dict))
    return HostAnalysis(host_name=host.name, host_url=host.url, is_local=local, reachable=reachable,
                         version=version, loaded=tuple(loaded), gpu=gpu, gpu_cards=gpu_cards,
                         catalog_fits=catalog_fits)


def _model_fit_info(host, catalog: dict, row: dict, *, state_dir, remote: bool, hw_runner=None) -> ModelFitInfo:
    """One `ModelFitInfo` row (round 5b, brief item 3's "what fits"
    column + learned-cap + source phrase), reusing the EXACT SAME
    `resolve_num_ctx_and_source` precedence `agent/loop.py`'s real
    request path runs -- this panel can never quietly disagree with what
    a real turn would actually send."""
    from halo_harness.providers.ollama_calibrate import get_last_turn_throughput, lookup_learned_cap
    name = row.get("model") or row.get("name") or "?"
    trained = trained_context_for(catalog, name)
    learned_cap = lookup_learned_cap(state_dir, host_url=host.url, model=name, digest=row.get("digest"))
    fit = estimate_fit_for_host(host, name, catalog, runner=hw_runner)
    what_fits, source = resolve_num_ctx_and_source(trained, host.max_ctx, fit, learned_cap=learned_cap,
                                                    remote=remote)
    throughput = get_last_turn_throughput(state_dir, host_url=host.url, model=name)
    return ModelFitInfo(name=name, learned_cap=learned_cap, what_fits=what_fits, source=source,
                         throughput=throughput)


def format_host_analysis(a: HostAnalysis) -> str:
    """The shared plain-text rendering `halo ollama`, `/ollama`'s print-
    mode fallback, and the TUI dialog's body all use verbatim."""
    lines = [f"Ollama host '{a.host_name}' ({a.host_url})"]
    if not a.reachable:
        lines.append("  unreachable")
        return "\n".join(lines)
    lines.append(f"  version {a.version or '?'}")
    if len(a.gpu_cards) > 1:
        total_free = sum(c.free_bytes for c in a.gpu_cards if isinstance(c.free_bytes, int))
        total_cap = sum(c.total_bytes for c in a.gpu_cards if isinstance(c.total_bytes, int))
        lines.append(f"  GPU: {len(a.gpu_cards)} cards, {human_bytes(total_free)} free of "
                     f"{human_bytes(total_cap)} combined (sum, not minimum -- round 5b multi-GPU fit)")
    elif a.gpu is not None:
        label = f"GPU ({a.gpu.vendor}{', ' + a.gpu.name if a.gpu.name else ''})"
        if getattr(a.gpu, "estimated", False):
            label += " [estimate -- `halo ollama calibrate` is the ground truth]"
        elif not a.is_local:
            label += " [via ssh]"
        lines.append(f"  {label}: {human_bytes(a.gpu.free_bytes)} free of {human_bytes(a.gpu.total_bytes)}")
    elif a.is_local:
        lines.append("  GPU memory: unknown (no supported OS GPU tool found, or it failed)")
    else:
        lines.append("  GPU memory: not read (remote host, no `ssh:` configured -- inferred from /api/ps only)")
    if not a.loaded:
        lines.append("  no models currently loaded")
    else:
        lines.append(f"  {len(a.loaded)} model(s) loaded:")
        for m in a.loaded:
            lines.append(f"    {m.offload_sentence}")
            trained = m.trained_context if m.trained_context is not None else "?"
            effective = m.effective_context if m.effective_context is not None else "?"
            kv = f"{m.kv_bytes_per_token:.0f} bytes/token" if m.kv_bytes_per_token else "unknown"
            lines.append(f"      trained context {trained}, loaded at {effective}, KV {kv}, "
                         f"tools_max {m.tools_max} (~{m.catalog_prompt_tokens} prompt tokens)")
    if a.catalog_fits:
        lines.append(f"  what fits ({len(a.catalog_fits)} model(s) in the catalog):")
        for fit_info in a.catalog_fits:
            cap_str = f"{fit_info.learned_cap} (learned)" if fit_info.learned_cap else "not calibrated"
            lines.append(f"    {fit_info.name}: num_ctx {fit_info.what_fits} [{fit_info.source}], "
                         f"learned cap: {cap_str}")
            if fit_info.throughput:
                tp = fit_info.throughput
                tps = tp.get("tokens_per_second")
                prefill = tp.get("prefill_seconds")
                offloaded = " (offloaded)" if tp.get("offloaded") else ""
                if tps or prefill:
                    lines.append(f"      last turn: {tps or '?'} tok/s, prefill {prefill or '?'} s{offloaded}")
    lines.append(f"  {KV_FORMULA_ORIGIN_NOTE}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Halo 2.0.3 round 5b part 2 (brief item 4): the host-setup checklist
# `halo ollama doctor [--host NAME]` and `halo doctor`'s own Ollama section
# both render verbatim -- "what the host exposes" is `format_host_analysis`
# above, unchanged; this is only the SECOND half, the documented
# recommendations Halo cannot read back from any API, plus where each one
# lives per OS. Plain sentences, never a block, per house policy.
# ---------------------------------------------------------------------------

_EXPOSE_SWITCH_SHORT = {
    "win32": "the tray app's \"Expose Ollama to the network\" switch",
    "darwin": "the menu-bar app's \"Expose to network\" switch",
    "linux": "OLLAMA_HOST=0.0.0.0 via `sudo systemctl edit ollama`",
}

_WHERE_SENTENCES = {
    "win32": ("Windows: the tray app's \"Expose Ollama to the network\" switch (its Settings screen) "
              "overrides OLLAMA_HOST outright; setting OLLAMA_HOST yourself as a user-scope environment "
              "variable (Settings -> Environment Variables) also works, but only takes effect after quitting "
              "and relaunching the tray app."),
    "darwin": ("macOS: the menu-bar app's own \"Expose to network\" switch overrides OLLAMA_HOST the same "
               "way; setting it yourself is `launchctl setenv OLLAMA_HOST 0.0.0.0:11434` followed by "
               "relaunching the app -- from the research doc's community knowledge (docs/harness/"
               "LOCAL-MODELS-RESEARCH.md section 4), not independently confirmed live."),
    "linux": ("Linux: `sudo systemctl edit ollama`, add one `Environment=\"VAR=value\"` line per variable "
              "under `[Service]`, then `sudo systemctl daemon-reload` and restart the service."),
}

_RECOMMENDATIONS = (
    "OLLAMA_FLASH_ATTENTION=1 (faster attention; also needed on most backends before KV cache "
    "quantization is honoured at all)",
    "OLLAMA_KV_CACHE_TYPE=q8_0 (roughly half the KV-cache memory of the f16 default, at a small quality cost)",
    "OLLAMA_NUM_PARALLEL=1 for a single-user box (never needs more than one request in flight at once; "
    "a higher value just splits the SAME context budget across concurrent requests)",
    "OLLAMA_KEEP_ALIVE (how long a model stays loaded after its last request -- longer avoids a reload "
    "on the next turn, at the cost of holding memory meanwhile)",
    "OLLAMA_CONTEXT_LENGTH (the server-wide context default applied when nothing else sets num_ctx -- "
    "Halo itself always sends num_ctx per request, so this mostly matters for other clients of the same host)",
)


def host_setup_checklist(host, *, platform_name: "Optional[str]" = None) -> "list[str]":
    """Plain sentences for `halo ollama doctor`/`halo doctor`'s Ollama
    section: the loopback-only one-liner (brief: "A loopback-only host
    gets one plain line") when `providers.ollama_hw.is_local_host(host)`,
    then the documented recommendations Halo cannot read back, then
    WHERE each one lives -- for a LOCAL host, only `platform_name`'s own
    OS (default `sys.platform`, the OS Halo itself is running on); for a
    REMOTE host (no `ssh:`-probed OS identity exists anywhere in this
    codebase), all three OSes briefly, since Halo genuinely does not know
    which one that host runs. `platform_name`, when given explicitly,
    overrides the local-host default -- the test seam (never read
    directly from `sys.platform` by a caller)."""
    from halo_harness.providers.ollama_hw import is_local_host
    local = is_local_host(host)
    lines: "list[str]" = []
    if local:
        import sys
        this_os = platform_name if platform_name is not None else sys.platform
        switch = _EXPOSE_SWITCH_SHORT.get(this_os, "the per-OS \"expose to network\" switch")
        lines.append(f"this daemon is reachable from this machine only; to share it on the LAN flip {switch}.")
        oses = (this_os,) if this_os in _WHERE_SENTENCES else ()
    else:
        oses = ("win32", "darwin", "linux")  # remote: unknown OS, print all three briefly
    lines.append("Halo cannot read these back from any API -- documented recommendations, not probed "
                 "facts: " + "; ".join(_RECOMMENDATIONS) + ".")
    for p in oses:
        lines.append(_WHERE_SENTENCES[p])
    return lines

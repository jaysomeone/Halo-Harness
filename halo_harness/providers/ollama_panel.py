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

from halo_harness.providers.ollama import fetch_ps, get_catalog, probe_version, trained_context_for
from halo_harness.providers.ollama_fit import estimate_catalog_prompt_tokens, kv_bytes_per_token, resolve_ollama_tools_max
from halo_harness.providers.ollama_hw import GpuMemory, catalog_row, get_local_gpu_memory, is_local_host

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
class HostAnalysis:
    host_name: str
    host_url: str
    is_local: bool
    reachable: bool
    version: Optional[str]
    loaded: "tuple"  # tuple[LoadedModelAnalysis, ...]
    gpu: Optional[GpuMemory]


def analyze_host(host, *, hw_runner=None, force: bool = False) -> HostAnalysis:
    """One host's full picture: reachability + version (`probe_version`),
    loaded models (`/api/ps`, cross-referenced against the catalog's
    `model_info` for trained context and the KV formula), and -- LOCAL
    hosts only -- OS-level GPU memory; a REMOTE host's `gpu` field is
    always `None` (research doc section 3: there is no OS tool to shell
    out to on a machine Halo isn't running on)."""
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
    gpu = get_local_gpu_memory(runner=hw_runner) if local else None
    return HostAnalysis(host_name=host.name, host_url=host.url, is_local=local, reachable=reachable,
                         version=version, loaded=tuple(loaded), gpu=gpu)


def format_host_analysis(a: HostAnalysis) -> str:
    """The shared plain-text rendering `halo ollama`, `/ollama`'s print-
    mode fallback, and the TUI dialog's body all use verbatim."""
    lines = [f"Ollama host '{a.host_name}' ({a.host_url})"]
    if not a.reachable:
        lines.append("  unreachable")
        return "\n".join(lines)
    lines.append(f"  version {a.version or '?'}")
    if a.gpu is not None:
        lines.append(f"  GPU ({a.gpu.vendor}{', ' + a.gpu.name if a.gpu.name else ''}): "
                     f"{human_bytes(a.gpu.free_bytes)} free of {human_bytes(a.gpu.total_bytes)}")
    elif a.is_local:
        lines.append("  GPU memory: unknown (no supported OS GPU tool found, or it failed)")
    else:
        lines.append("  GPU memory: not read (remote host -- inferred from /api/ps only)")
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
    lines.append(f"  {KV_FORMULA_ORIGIN_NOTE}")
    return "\n".join(lines)

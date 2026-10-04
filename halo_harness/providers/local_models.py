"""halo_harness.providers.local_models -- Halo 2.0.3 round 5: the shared
`/local` discovery view. `build_local_view` merges THREE sources, in this
fixed order (round 5 brief item 4): (1) each configured Ollama host's
catalog (round 3's `providers.ollama_panel.analyze_host`, loaded-now and
fit included), (2) running Hugging Face local servers -- auto-detected
PLUS manual `huggingface.local_servers` entries, and (3) the Hugging Face
Hub cache -- models on disk but not necessarily served by anything right
now. `halo local` (`local_cli.py`), `/local`'s print-mode fallback
(`commands/builtins.py`) and the TUI dialog (`tui/dialogs/local_status.py`)
all render the SAME `build_local_view`/`format_local_view` pair, so none of
the three can quietly disagree -- the exact reason `ollama_panel.py` is
split the same way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("bridge")


@dataclass(frozen=True)
class LocalModelRow:
    """One row. `ref`, when set, is a usable `--model`/`/model` string;
    `None` means this row isn't independently addressable (a hub-cache
    entry with nothing serving it, or an auto-detected server beyond the
    FIRST one -- only the first auto-detected server is what a bare
    `hf:local/<model>` ref actually resolves to; a second one has no
    stable name of its own to address it by). `reachable`: `True`/`False`
    once actually probed this call, `None` when not probed at all (a
    manual entry on a bare, non-refresh `/local` open -- brief item 1/4:
    "a manual entry is never probed in the background unless the user
    asks")."""
    group: str
    name: str
    ref: Optional[str] = None
    size_bytes: Optional[int] = None
    quant: Optional[str] = None
    context: Optional[int] = None
    capability: str = "declared: unknown"
    loaded: Optional[bool] = None
    reachable: Optional[bool] = None


def _grouped(rows: "list[LocalModelRow]") -> "list[tuple[str, list[LocalModelRow]]]":
    """Stable-groups by `.group`, preserving first-seen order -- same
    small algorithm `tui/dialogs/model_picker.py::_grouped` uses for the
    `/model` picker, reimplemented here (never imported FROM a `tui/`
    module into `providers/`) since this module's two callers (the CLI and
    the plain-text `/local` fallback) have no Textual dependency at all."""
    order: "list[str]" = []
    buckets: "dict[str, list[LocalModelRow]]" = {}
    for r in rows:
        if r.group not in buckets:
            buckets[r.group] = []
            order.append(r.group)
        buckets[r.group].append(r)
    return [(g, buckets[g]) for g in order]


def _capability_text(*, declared: bool, probed: Optional[bool]) -> str:
    """Round 5 brief item 4: "declared vs probed, reuse round 2's
    capability-probe idea only where a probe exists; otherwise declared"
    -- `probed` is a read-ONLY lookup of round 2's existing capability
    cache (never a new, costly real inference call just from opening
    `/local`); `None` means "never probed", which falls back to the
    catalog's own declared claim."""
    if probed is not None:
        return f"tools: probed {'yes' if probed else 'no'}"
    return f"tools: declared {'yes' if declared else 'no'}"


def _ollama_rows(host, *, refresh: bool, hw_runner, state_dir) -> "list[LocalModelRow]":
    from halo_harness.providers.ollama_capability import load_capability_cache
    from halo_harness.providers.ollama_panel import analyze_host
    group = f"Ollama ({host.name})"
    analysis = analyze_host(host, hw_runner=hw_runner, force=refresh)
    if not analysis.reachable:
        return [LocalModelRow(group=group, name="(unreachable)", reachable=False)]
    from halo_harness.providers.ollama import get_catalog, trained_context_for
    catalog = get_catalog(host)
    cap_cache = load_capability_cache(state_dir)
    loaded_by_name = {m.name: m for m in analysis.loaded}
    out = []
    for row in catalog.get("models") or []:
        name = row.get("model") or row.get("name")
        if not name:
            continue
        details = row.get("details") or {}
        probed = cap_cache.get(row.get("digest"), {}).get("tool_calls") if row.get("digest") else None
        cap_text = _capability_text(declared="tools" in (row.get("capabilities") or []), probed=probed)
        loaded_entry = loaded_by_name.get(name)
        context = loaded_entry.effective_context if loaded_entry else trained_context_for(catalog, name)
        ref = f"ol:{name}" if host.default else f"ol:{name}@{host.name}"
        out.append(LocalModelRow(group=group, name=name, ref=ref,
                                  size_bytes=row.get("size") if isinstance(row.get("size"), int) else None,
                                  quant=details.get("quantization_level"), context=context, capability=cap_text,
                                  loaded=loaded_entry is not None, reachable=True))
    return out


def _hf_server_rows(info, *, reachable: bool, addressable: bool) -> "list[LocalModelRow]":
    # Round 5b part 2 (brief item 6): "label 'MLX' in /local" --
    # `info.runtime_label` ("llama-server"/"mlx"/None, `providers.
    # huggingface_local_probe._runtime_label_for`'s own docstring covers
    # exactly how confident each value is) rides along in the group name,
    # the same place `ollama_panel`'s own `(vendor)` GPU label already
    # lives for a comparable "what is this, really" fact.
    label = f", {getattr(info, 'runtime_label', None).upper()}" if getattr(info, "runtime_label", None) == "mlx" \
        else ""
    group = f"Hugging Face (local: {info.name}{label})"
    if not info.model_ids:
        return [LocalModelRow(group=group, name="(no models reported)", reachable=reachable)]
    out = []
    for mid in info.model_ids:
        ref = None
        if addressable:
            ref = f"hf:local/{mid}@{info.name}" if info.manual else f"hf:local/{mid}"
        out.append(LocalModelRow(group=group, name=mid, ref=ref, context=info.context_by_model.get(mid),
                                  capability="declared: unknown (not reported by /v1/models)", reachable=reachable))
    return out


def build_local_view(*, refresh: bool = False, env: Optional[dict] = None, state_dir=None,
                      hw_runner=None) -> "list[LocalModelRow]":
    """The merged list -- see this module's own docstring for the fixed
    three-source order. `refresh`: force Ollama's own catalog re-read AND
    probe every configured manual `huggingface.local_servers` entry (bare
    `/local` skips both -- the Ollama catalog's own short TTL cache
    answers instantly, and a manual entry shows "configured, not probed"
    instead of a live reachable/model list). Auto-detection always runs
    (gated only by `BRIDGE_TEST_NO_BACKGROUND_NET`, never by `refresh`)."""
    if state_dir is None:
        from halo_harness.config.paths import bridge_home
        state_dir = bridge_home()
    rows: "list[LocalModelRow]" = []
    from halo_harness.providers.ollama import resolve_ollama_hosts
    for host in resolve_ollama_hosts(env):
        rows.extend(_ollama_rows(host, refresh=refresh, hw_runner=hw_runner, state_dir=state_dir))
    from halo_harness.providers.huggingface import resolve_huggingface_local_servers
    from halo_harness.providers.huggingface_local_probe import auto_detect_local_servers, probe_manual_server
    detected = auto_detect_local_servers(env=env)
    for i, info in enumerate(detected):
        rows.extend(_hf_server_rows(info, reachable=True, addressable=(i == 0)))
    for server in resolve_huggingface_local_servers():
        if refresh:
            info = probe_manual_server(server)
            if info is not None:
                rows.extend(_hf_server_rows(info, reachable=True, addressable=True))
            else:
                rows.append(LocalModelRow(group=f"Hugging Face (local: {server.name})",
                                           name="(unreachable)", reachable=False))
        else:
            rows.append(LocalModelRow(group=f"Hugging Face (local: {server.name})",
                                       name="(configured -- /local refresh to check)",
                                       ref=f"hf:local/<model>@{server.name}", reachable=None))
    from halo_harness.providers.huggingface_hub_cache import scan_hub_cache
    for m in scan_hub_cache(env=env):
        out_name = m.repo_id + (f" ({'/'.join(m.formats)})" if m.formats else "")
        # Round 5b part 2 (brief item 6): "MLX repos in the hub cache
        # (mlx-community/*) labelled as runnable through MLX" -- a plain
        # org-name check (research doc section 7's own framing of
        # `mlx-community` as the Hub org MLX-quantized repos live under),
        # never a guess at the repo's OWN internal format.
        cap = "declared: unknown (not currently served)"
        if m.repo_id.lower().startswith("mlx-community/"):
            cap += " -- runnable through MLX"
        rows.append(LocalModelRow(group="Hugging Face (cache, not served)", name=out_name, size_bytes=m.size_bytes,
                                   capability=cap))
    from halo_harness.providers.lmstudio_cache import scan_lmstudio_models
    for m in scan_lmstudio_models(env=env):
        out_name = m.repo_id + (f" ({'/'.join(m.formats)})" if m.formats else "")
        rows.append(LocalModelRow(group="LM Studio (cache, not served)", name=out_name, size_bytes=m.size_bytes,
                                   capability="declared: unknown (not currently served)"))
    return rows


def _format_row(r: LocalModelRow) -> str:
    from halo_harness.providers.ollama_panel import human_bytes
    parts = [r.name]
    if r.size_bytes is not None:
        parts.append(human_bytes(r.size_bytes))
    if r.quant:
        parts.append(r.quant)
    if r.context is not None:
        parts.append(f"ctx={r.context}")
    parts.append(r.capability)
    if r.loaded is not None:
        parts.append("loaded" if r.loaded else "not loaded")
    if r.reachable is False:
        parts.append("unreachable")
    elif r.reachable is None and r.ref is not None:
        parts.append("not probed")
    if r.ref:
        parts.append(f"[{r.ref}]")
    return "  ".join(parts)


def format_local_view(rows: "list[LocalModelRow]") -> str:
    """The shared plain-text rendering `halo local`, `/local`'s print-mode
    fallback, and the TUI dialog's body all use verbatim."""
    if not rows:
        return ("No local models found -- no Ollama host configured/reachable, no Hugging Face local server "
                "detected or configured, nothing in the Hugging Face Hub cache.")
    lines = []
    for i, (group, members) in enumerate(_grouped(rows)):
        if i:
            lines.append("")
        lines.append(f"{group}:")
        for r in members:
            lines.append("  " + _format_row(r))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Halo 2.0.3 round 5b part 2 (brief item 5, "the wizard detects before it
# asks"): the init tab's own opening detection summary -- reuses every
# probe this module/`ollama_panel.analyze_host` already runs for `/local`/
# `/ollama`, never a fourth copy of any of them. Plain sentences, no
# downloads, no block.
# ---------------------------------------------------------------------------

# Illustrative only (NOT measured on any real model this round) -- rough
# dense-model parameter counts for the size classes rolo's own machines and
# the model-table.json catalog actually see in practice. Largest-first so
# `what_fits_at_32k` can report the biggest class(es) that fit.
_ILLUSTRATIVE_MODEL_CLASSES = (
    ("70B dense", 70.0),
    ("30-32B dense/MoE", 32.0),
    ("14B dense", 14.0),
    ("7-8B dense", 8.0),
)
# A flat reservation for a 32k-token KV cache, not derived per-model (no
# architecture is known yet for a model that isn't installed) -- illustrative,
# documented as such in `what_fits_at_32k`'s own docstring and in docs/MODELS.md.
_WIZARD_32K_RESERVE_BYTES = 2 * 1024 ** 3


def what_fits_at_32k(free_bytes: "Optional[int]") -> "list[str]":
    """One or two "<class> at <quant>" strings (brief item 5: "one or two
    model classes and quantizations that would fit with a 32k context")
    for `free_bytes` of free GPU/unified memory -- `[]` for `None`/non-
    positive input. The WEIGHT side of the check is the real, exact
    per-parameter byte count (`providers.ollama_fit.KV_BYTES_PER_ELEM`,
    the SAME table the KV-cache arithmetic uses, reused here because GGUF
    weight tensors and KV cache entries share the identical block
    quantization scheme); the 32k-CONTEXT side is a single flat
    reservation (`_WIZARD_32K_RESERVE_BYTES`, ~2 GiB), NOT this model
    class's own KV-bytes/token formula -- there is no real architecture
    (block_count/head_count_kv/embedding_length) to compute that formula
    from for a model that isn't installed yet. Labelled an estimate by
    the caller (`detection_summary_lines`), never presented as measured;
    `halo ollama calibrate` after actually installing one is the ground
    truth, same as every other first-guess in this round."""
    if not isinstance(free_bytes, int) or isinstance(free_bytes, bool) or free_bytes <= 0:
        return []
    budget = free_bytes - _WIZARD_32K_RESERVE_BYTES
    if budget <= 0:
        return []
    from halo_harness.providers.ollama_fit import KV_BYTES_PER_ELEM
    out: "list[str]" = []
    for label, params_billions in _ILLUSTRATIVE_MODEL_CLASSES:
        for quant in ("q4_0", "q8_0"):
            weight_bytes = int(params_billions * 1_000_000_000 * KV_BYTES_PER_ELEM[quant])
            if weight_bytes <= budget:
                out.append(f"{label} at {quant}")
                break
        if len(out) >= 2:
            break
    return out


def detection_summary_lines(*, env: Optional[dict] = None, state_dir=None, hw_runner=None) -> "list[str]":
    """Plain sentences for the Ollama/Hugging Face init-wizard tabs' own
    opening summary (brief item 5): GPU/unified memory with the estimate
    label, what fits now per installed Ollama model (learned cap or fit
    estimate, labelled -- reusing `ollama_panel.analyze_host`'s own
    `catalog_fits`, never a second computation of it), running local
    servers, and model folders known so far (reusing `build_local_view`
    for both of the last two). Intended to run inside a background
    worker (never the UI thread) with a short overall budget -- every
    probe it calls already carries its own short timeout and already
    honours `BRIDGE_TEST_NO_BACKGROUND_NET`; this function adds no new
    network call of its own beyond composing their results. Never raises
    -- a failure in any one section is swallowed and simply omits that
    section's lines, so a slow/broken probe degrades the summary, never
    the whole wizard."""
    lines: "list[str]" = []
    try:
        from halo_harness.providers.ollama_hw import get_local_gpu_memory
        from halo_harness.providers.ollama_panel import human_bytes
        gpu = get_local_gpu_memory(runner=hw_runner)
        if gpu is None:
            lines.append("No local GPU or unified memory could be detected (no supported OS tool found, or none ran).")
        else:
            est = " [estimate -- `halo ollama calibrate` after installing a model is the ground truth]" \
                if getattr(gpu, "estimated", False) else ""
            name = f" {gpu.name}" if gpu.name else ""
            lines.append(f"GPU/unified memory ({gpu.vendor}{name}){est}: {human_bytes(gpu.free_bytes)} free of "
                         f"{human_bytes(gpu.total_bytes)}.")
            fits = what_fits_at_32k(gpu.free_bytes)
            if fits:
                lines.append("With that much free, " + " or ".join(fits) + " would likely fit with a 32k "
                             "context (a rough estimate from the weight/KV-cache arithmetic, not a "
                             "measurement of any specific model).")
    except Exception:
        log.debug("local_models: GPU detection for the wizard summary failed", exc_info=True)
    try:
        from halo_harness.providers.ollama import resolve_ollama_host
        from halo_harness.providers.ollama_panel import analyze_host
        host = resolve_ollama_host(None, env)
        if host is not None:
            analysis = analyze_host(host, hw_runner=hw_runner, state_dir=state_dir)
            if not analysis.reachable:
                lines.append(f"Ollama is not reachable at {host.url} yet.")
            else:
                model_names = [m.name for m in analysis.catalog_fits]
                lines.append(f"Ollama is reachable at {host.url} (version {analysis.version or '?'}); "
                             f"{len(model_names)} model(s): {', '.join(model_names) or '(none pulled yet)'}.")
                for fit_info in analysis.catalog_fits:
                    label = "learned cap" if fit_info.learned_cap else fit_info.source
                    ctx = fit_info.learned_cap or fit_info.what_fits
                    lines.append(f"  {fit_info.name}: largest fully-resident context ~{ctx} ({label}).")
    except Exception:
        log.debug("local_models: Ollama detection for the wizard summary failed", exc_info=True)
    try:
        rows = build_local_view(env=env, state_dir=state_dir, hw_runner=hw_runner)
        server_groups = sorted({r.group for r in rows if r.group.startswith("Hugging Face (local:")})
        if server_groups:
            names = [g.split("local: ", 1)[-1].rstrip(")") for g in server_groups]
            lines.append(f"Running local server(s): {', '.join(names)}.")
        else:
            lines.append("No running local OpenAI-compatible server (llama-server, vLLM, LM Studio, MLX, ...) "
                         "detected yet.")
        cache_rows = [r for r in rows
                      if r.group in ("Hugging Face (cache, not served)", "LM Studio (cache, not served)")]
        if cache_rows:
            lines.append(f"Model folders known so far: {len(cache_rows)} model(s) on disk, not currently served.")
    except Exception:
        log.debug("local_models: server/cache detection for the wizard summary failed", exc_info=True)
    return lines

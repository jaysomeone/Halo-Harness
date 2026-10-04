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

from dataclasses import dataclass
from typing import Optional


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
    group = f"Hugging Face (local: {info.name})"
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
        rows.append(LocalModelRow(group="Hugging Face (cache, not served)", name=out_name, size_bytes=m.size_bytes,
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

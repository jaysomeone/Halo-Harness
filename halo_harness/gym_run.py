"""halo_harness.gym_run -- Halo 2.0.3 round 5d: orchestrates one model's
full gym battery (`gym_tool_tasks.py`/`gym_reply_tasks.py`) into one
`gym.GymResult`, and the multi-model `halo gym` entry point. This module
is deliberately the only one that resolves a model ref against a REAL
Ollama host/catalog and writes the result file -- every task module
above it only ever takes an already-resolved `host`/`route`/`profile`/
`decision`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from halo_harness.gym import GymResult, battery_n, context_recall_n, save_result
from halo_harness.gym_reply_tasks import run_context_recall_task, run_instruction_adherence_task
from halo_harness.gym_tool_tasks import run_edit_success_task, run_tool_call_accuracy_task


def _average_throughput(timing_samples: "list[dict]") -> "tuple[Optional[float], Optional[float]]":
    """The identical formula `agent/loop.py::_record_ollama_throughput`
    uses for the status bar (`eval_count`/`eval_duration` -> tok/s,
    `prompt_eval_duration` -> prefill seconds, both nanoseconds) --
    averaged across every PRIMARY battery turn this run actually sent
    (repair-round turns are excluded on purpose: a smaller, schema-
    constrained completion would skew the average toward an unrelated
    number)."""
    tps_values, prefill_values = [], []
    for t in timing_samples:
        count, duration = t.get("eval_count"), t.get("eval_duration")
        if isinstance(count, int) and isinstance(duration, (int, float)) and duration > 0:
            tps_values.append(count / (duration / 1_000_000_000.0))
        prompt_duration = t.get("prompt_eval_duration")
        if isinstance(prompt_duration, (int, float)) and prompt_duration >= 0:
            prefill_values.append(prompt_duration / 1_000_000_000.0)
    tps = round(sum(tps_values) / len(tps_values), 1) if tps_values else None
    prefill = round(sum(prefill_values) / len(prefill_values), 2) if prefill_values else None
    return tps, prefill


def _run_battery(result: GymResult, *, host, route, profile, decision, quick: bool, scratch: Path,
                  state_dir) -> GymResult:
    """Runs all four battery tasks against an already-resolved host/
    route/profile/decision and fills `result` in place -- shared by the
    ollama and huggingface branches of `run_gym_for_model` below so
    neither duplicates the other's task-running/scoring-assembly logic.
    Every task call goes through `send_turn_for` (inside each task
    function, unchanged by this round) -- this function itself never
    knows or cares which provider `route` names."""
    n, cn = battery_n(quick), context_recall_n(quick)
    timing: "list[dict]" = []
    errors: "list[str]" = []

    def _run(label, fn, **kwargs):
        try:
            return fn(host=host, route=route, profile=profile, decision=decision, state_dir=state_dir,
                      timing=timing, **kwargs)
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}: {e}")
            return None

    tca = _run("tool_call_accuracy", run_tool_call_accuracy_task, scratch_dir=scratch, n=n)
    edit = _run("edit_success", run_edit_success_task, scratch_dir=scratch, n=n)
    recall = _run("context_recall", run_context_recall_task, n=cn, quick=quick)
    instr = _run("instruction_adherence", run_instruction_adherence_task, n=n)

    if tca is not None:
        result.tool_call_accuracy = tca
    if edit is not None:
        result.edit_success = edit
    if recall is not None:
        result.context_recall = recall
    if instr is not None:
        result.instruction_adherence = instr
    result.errors = errors
    result.tokens_per_second, result.prefill_seconds = _average_throughput(timing)
    result.turns = len(timing) + result.tool_call_accuracy.repair_rounds
    result.finished_at = time.time()
    return result


def _scratch_dir(scratch_dir) -> Path:
    import tempfile
    scratch = Path(scratch_dir) if scratch_dir else Path(tempfile.mkdtemp(prefix="halo-gym-"))
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _run_gym_for_ollama_model(ref, model_ref_raw: str, *, quick: bool, state_dir, scratch_dir) -> dict:
    from halo_harness.providers.ollama import get_catalog, probe_version, resolve_ollama_host
    from halo_harness.providers.ollama_hw import catalog_row, resolve_context_decision
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.routing import Route

    started = time.time()
    host = resolve_ollama_host(ref.host)
    if host is None:
        return GymResult(model=ref.model, model_ref=model_ref_raw, host_name=ref.host or "?", host_url="?",
                          started_at=started, finished_at=started, quick=quick,
                          errors=[f"no configured Ollama host for {model_ref_raw!r} -- see `ollama.hosts` "
                                  f"in ~/.halo/config.json"]).to_dict()

    catalog = get_catalog(host)
    row = catalog_row(catalog, ref.model) or {}
    version_info = probe_version(host)
    decision = resolve_context_decision(ref)
    route = Route(provider="ollama", upstream_model=ref.model, dialect="ollama")
    profile = resolve_profile(route, state_dir=state_dir)

    result = GymResult(
        model=ref.model, model_ref=model_ref_raw, host_name=host.name, host_url=host.url,
        digest=row.get("digest"), quantization=(row.get("details") or {}).get("quantization_level"),
        fitted_context=decision.num_ctx, ollama_version=(version_info or {}).get("version"),
        started_at=started, quick=quick,
    )
    result = _run_battery(result, host=host, route=route, profile=profile, decision=decision, quick=quick,
                           scratch=_scratch_dir(scratch_dir), state_dir=state_dir)
    d = result.to_dict()
    save_result(state_dir, d)
    return d


def _resolve_huggingface_host(ref, model_ref_raw: str, state_dir):
    """`(host, stable_id, error)` for an `hf:local/*`/`hf:mlx/*` ref --
    mirrors `doctor_local.py`'s own (underscore-private) `_resolved_
    huggingface`/`_HFHost`, written fresh here rather than imported (that
    module is owned by a concurrent round this one does not edit).
    `stable_id` is the brief's own "keyed by a stable id" choice: the bare
    Hub repo id for `hf:mlx` (`ensure_mlx_server` ALREADY registers/names
    its managed server by that exact id), the resolved server's own name
    for a generic `hf:local` ref (no per-model digest concept exists for
    an arbitrary OpenAI-compatible server, so the SERVER is the measured
    unit there). Refuses an `hf:` ref that is neither (a router ref, or
    `hf:endpoint/<name>`) -- the gym's battery only ever runs against a
    reachable LOCAL model, same policy the ollama branch already has for
    a non-`ollama` provider."""
    from halo_harness.gym_send import HFHost
    if ref.mlx:
        from halo_harness.providers.huggingface_mlx import ensure_mlx_server
        target, lines = ensure_mlx_server(ref.model, state_dir=state_dir)
        if target is None:
            return None, None, ("; ".join(lines) or f"could not start a managed mlx_lm.server for {model_ref_raw!r}")
        return HFHost(base_url=target.base_url, api_key=target.api_key or "", name=target.name), ref.model, None
    if ref.local:
        from halo_harness.providers.huggingface_local_resolve import resolve_local_server
        target = resolve_local_server(ref.host, env=None)
        if target is None:
            return None, None, f"no Hugging Face local server resolved for {model_ref_raw!r}"
        return HFHost(base_url=target.base_url, api_key=target.api_key or "", name=target.name), target.name, None
    return None, None, (f"{model_ref_raw!r} is not a local ol:/hf:local/hf:mlx model -- this round's battery "
                         f"only runs against a local model (a cloud/router/endpoint ref can still be compared "
                         f"via `gym show`/`gym propose` once scored some other way)")


def _run_gym_for_huggingface_model(ref, model_ref_raw: str, *, quick: bool, state_dir, scratch_dir) -> dict:
    from halo_harness.gym_send import HFContextDecision
    from halo_harness.model import resolve_model_profile
    from halo_harness.providers.routing import Route

    started = time.time()
    host, stable_id, error = _resolve_huggingface_host(ref, model_ref_raw, state_dir)
    if error:
        return GymResult(model=ref.model, model_ref=model_ref_raw, host_name="huggingface", host_url="?",
                          started_at=started, finished_at=started, quick=quick, errors=[error]).to_dict()

    route = Route(provider="huggingface", upstream_model=ref.model, dialect="openai-chat")
    # brief: "recall sized to the server's reported context or the
    # profile default" -- `resolve_model_profile` IS that exact fallback
    # chain (`providers.huggingface_local_resolve.cached_local_context_
    # tokens` -- the server's OWN `/v1/models`/`/props` report -- else
    # `ModelProfile`'s bare 128000 default); never re-derived here.
    profile_obj = resolve_model_profile(ref, state_dir)
    decision = HFContextDecision(num_ctx=profile_obj.context_tokens, max_output_tokens=profile_obj.max_output_tokens)

    # Grouped under one shared "huggingface" host-slug directory (never
    # one folder per model/server) -- `stable_id` (repo id or server
    # name) is what tells two different hf: measurements apart WITHIN
    # it, the same role `digest` plays for Ollama's per-host catalog.
    result = GymResult(
        model=ref.model, model_ref=model_ref_raw, host_name="huggingface", host_url=host.base_url,
        digest=stable_id, fitted_context=decision.num_ctx, started_at=started, quick=quick,
    )
    result = _run_battery(result, host=host, route=route, profile=None, decision=decision, quick=quick,
                           scratch=_scratch_dir(scratch_dir), state_dir=state_dir)
    d = result.to_dict()
    save_result(state_dir, d)
    return d


def run_gym_for_model(model_ref_raw: str, *, quick: bool = False, state_dir, scratch_dir=None) -> dict:
    """Runs the whole battery against ONE model ref and returns/saves its
    `gym.GymResult.to_dict()`. Never raises: a host/catalog that can't be
    reached at all comes back as a result with every score `None` and one
    plain line in `errors`, the same "describe, never crash" contract
    every task function below it already keeps; an individual task's own
    unexpected exception is caught here too, so one broken task never
    loses the other three's real measurements. Dispatches on `ref.
    provider` -- `ol:` (round 5d, the original scope) or `hf:local/*`/
    `hf:mlx/*` (round 5i) -- anything else is refused with a plain error,
    never silently skipped."""
    from halo_harness.model import parse_model_ref
    ref = parse_model_ref(model_ref_raw)
    if ref.provider == "huggingface":
        return _run_gym_for_huggingface_model(ref, model_ref_raw, quick=quick, state_dir=state_dir,
                                               scratch_dir=scratch_dir)
    if ref.provider != "ollama":
        started = time.time()
        return GymResult(model=ref.model, model_ref=model_ref_raw, host_name="?", host_url="?",
                          started_at=started, finished_at=started, quick=quick,
                          errors=[f"{model_ref_raw!r} is not a local ol:/hf:local/hf:mlx model -- this round's "
                                  f"battery only runs against a local model (a cloud model can still be "
                                  f"compared via `gym show`/`gym propose` once scored some other way)"]).to_dict()
    return _run_gym_for_ollama_model(ref, model_ref_raw, quick=quick, state_dir=state_dir, scratch_dir=scratch_dir)


def default_model_refs(state_dir=None) -> "list[str]":
    """Brief item 1: "a fixed task battery run against EACH local model
    on this hardware" when `--models` is omitted -- every model in the
    DEFAULT Ollama host's own catalog (never a non-default host, and
    never `hf:*`/a cloud ref -- those need naming explicitly, per the
    brief's own "cloud models ... never included by default"). `[]` on an
    unreachable/unconfigured default host, never raises."""
    from halo_harness.providers.ollama import get_catalog, resolve_ollama_host
    host = resolve_ollama_host(None)
    if host is None:
        return []
    catalog = get_catalog(host)
    suffix = "" if (host.default or not host.name or host.name == "default") else f"@{host.name}"
    return [f"ol:{row.get('model') or row.get('name')}{suffix}" for row in (catalog.get("models") or [])
            if isinstance(row, dict) and (row.get("model") or row.get("name"))]


def run_gym(model_refs: "Optional[list]" = None, *, quick: bool = False, state_dir, on_each=None) -> "list[dict]":
    """`halo gym`'s own entry point: runs `run_gym_for_model` for every
    ref in `model_refs` (or `default_model_refs()` when omitted/empty),
    calling `on_each(ref, result_dict)` right after each one lands (CLI
    progress -- never required, `None` is a silent batch run, e.g. from a
    test)."""
    refs = list(model_refs) if model_refs else default_model_refs(state_dir)
    results = []
    for ref in refs:
        result = run_gym_for_model(ref, quick=quick, state_dir=state_dir)
        results.append(result)
        if on_each is not None:
            on_each(ref, result)
    return results

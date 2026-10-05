"""halo_harness.providers.ollama_calibrate -- Halo 2.0.3 round 5b item 1/2:
"fit calibration from Ollama's own telemetry, no GPU tool required."
`halo ollama calibrate <model> [--host NAME] [--start N]` (and the same
procedure run automatically the first time a model is used on a host with
no learned cap) loads a model at a candidate `num_ctx`, reads `/api/ps`
back (`size` vs `size_vram`), and steps down by powers of two until the
model is FULLY resident -- a measured fact, never a GPU-memory guess.
Results persist in `~/.halo/ollama-fit.json` (state dir), mirroring
`providers.ollama_capability`'s own cache-file pattern (atomic tmp+replace
write, `{}`/missing-file degrades to empty rather than raising).

A learned cap never expires on its own (brief item 2) -- it is re-measured
only by an explicit `halo ollama calibrate` or when the model's own
`digest` changes (a different `ollama pull`) or `ollama_version` changes
(a server upgrade may change how memory is actually used); `lookup_
learned_cap` treats either mismatch as "no learned cap at all" rather
than trusting a stale measurement. The SAME file also keeps the last real
turn's throughput numbers per (host, model) -- brief item 7, `halo ollama`
prints them -- since both are small, rarely-written, host+model-keyed
facts about one Ollama installation; no reason for a second file.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("bridge")

_FIT_STORE_FILENAME = "ollama-fit.json"


def _fit_store_path(state_dir) -> Path:
    return Path(state_dir) / _FIT_STORE_FILENAME


def load_fit_store(state_dir) -> dict:
    """`{"entries": [...], "last_turns": {...}}`; a fresh shape (never
    raises) when the file is missing, unreadable, or not a dict -- a lost
    store just means every host+model gets auto-calibrated/measured again,
    same cost as a fresh install."""
    path = _fit_store_path(state_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    if not isinstance(data.get("last_turns"), dict):
        data["last_turns"] = {}
    return data


def save_fit_store(state_dir, data: dict) -> None:
    path = _fit_store_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        import os
        os.replace(tmp, path)
    except OSError:
        log.debug("ollama: could not persist fit store to %s", path, exc_info=True)


def _find_entry(entries: list, *, host_url: str, model: str) -> Optional[dict]:
    for entry in entries:
        if isinstance(entry, dict) and entry.get("host_url") == host_url and entry.get("model") == model:
            return entry
    return None


def has_calibration_entry(state_dir, *, host_url: str, model: str) -> bool:
    """True iff SOME entry exists for (host_url, model), regardless of
    digest/version staleness or whether it found a fit at all -- this is
    what gates auto-calibration (brief: run it "the first time a model is
    used on a host with no learned cap"; a stale-but-present entry still
    means it's not the FIRST time, so auto-calibrate does not re-trigger
    on its own -- only an explicit `halo ollama calibrate` or a digest/
    version change does, per `lookup_learned_cap`'s own docstring)."""
    return _find_entry(load_fit_store(state_dir)["entries"], host_url=host_url, model=model) is not None


def lookup_learned_cap(state_dir, *, host_url: str, model: str, digest: Optional[str] = None,
                        ollama_version: Optional[str] = None) -> Optional[int]:
    """The learned `max_full_gpu_ctx` for (host_url, model), or `None`
    when there's no entry, the entry itself recorded "does not fit"
    (`max_full_gpu_ctx` stored as `None`), or -- a digest/version MISMATCH
    -- `digest`/`ollama_version` are given AND differ from what the entry
    recorded (a re-pulled model or an upgraded server invalidates a prior
    measurement; brief item 2: "re-measured... when the model digest or
    Ollama version changes"). `digest=None`/`ollama_version=None` (the
    live per-turn hot path, `providers.ollama_hw.resolve_context_decision`
    -- never pays for a fresh `/api/version` probe just for this) skips
    that specific check, trusting the entry on host_url+model alone."""
    entry = _find_entry(load_fit_store(state_dir)["entries"], host_url=host_url, model=model)
    if entry is None:
        return None
    if digest is not None and entry.get("digest") is not None and entry.get("digest") != digest:
        return None
    if ollama_version is not None and entry.get("ollama_version") is not None \
            and entry.get("ollama_version") != ollama_version:
        return None
    cap = entry.get("max_full_gpu_ctx")
    return cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else None


def record_calibration(state_dir, *, host_url: str, model: str, digest: Optional[str],
                        max_full_gpu_ctx: Optional[int], ollama_version: Optional[str]) -> dict:
    """Upserts the (host_url, model) entry -- `{host_url, model, digest,
    max_full_gpu_ctx, measured_at, ollama_version}` (brief item 2's exact
    schema). `max_full_gpu_ctx=None` is a VALID, meaningful recording
    ("measured: does not fit even at the floor") -- distinct from no entry
    at all -- so `has_calibration_entry` still returns True for it and
    auto-calibrate does not keep retrying every turn."""
    store = load_fit_store(state_dir)
    entries = store["entries"]
    entry = _find_entry(entries, host_url=host_url, model=model)
    record = {"host_url": host_url, "model": model, "digest": digest,
              "max_full_gpu_ctx": max_full_gpu_ctx, "measured_at": time.time(),
              "ollama_version": ollama_version}
    if entry is not None:
        entry.clear()
        entry.update(record)
    else:
        entries.append(record)
    save_fit_store(state_dir, store)
    return record


def auto_calibrate_enabled(env=None) -> bool:
    """`ollama.auto_calibrate` (default True) -- brief item 2: "opt out
    with `ollama.auto_calibrate: false`". Never raises on a typo'd
    config value (anything that isn't a plain bool is treated as "not
    explicitly disabled", i.e. True)."""
    del env  # accepted for call-site symmetry with other *_enabled helpers; config is process-wide, not per-env
    from halo_harness.theme import get_config_value
    value = get_config_value("ollama.auto_calibrate", default=True)
    return value is not False


MIN_CALIBRATE_CTX = 4096


def _floor_pow2(n: int) -> int:
    power = 1
    while power * 2 <= n:
        power *= 2
    return power


@dataclass(frozen=True)
class CalibrationResult:
    outcome: str  # "fits" | "does_not_fit"
    max_full_gpu_ctx: Optional[int]
    steps: int
    last_size: Optional[int] = None
    last_size_vram: Optional[int] = None


def _load_model_at_num_ctx(host, model: str, *, num_ctx: int, keep_alive: Optional[str], timeout: float) -> None:
    """One non-streaming `/api/chat` call that forces Ollama to (re)load
    `model` at `num_ctx` (`ollama_capability.py`'s own `_run_capability_
    probe` uses the identical `stream: False` shape against a real
    server). `num_predict: 1` keeps the generated-token cost to a single
    token -- the `/api/ps` read right after this call is what calibration
    actually acts on, not this response's own text, so the reply is
    discarded; any transport failure here is swallowed by `_get_json`
    itself (returns `None`) and simply means the FOLLOWING `/api/ps` read
    probably won't find this model loaded either -- `run_calibration`
    treats that exactly like a partial-offload reading, see its own
    docstring."""
    from halo_harness.providers.ollama import _get_json
    body = {"model": model, "stream": False, "messages": [{"role": "user", "content": "hi"}],
            "options": {"num_ctx": num_ctx, "num_predict": 1}}
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    _get_json(host, "/api/chat", method="POST", body=body, timeout=timeout)


def _ps_entry_for_model(ps: Optional[dict], model: str) -> Optional[dict]:
    """FIX PASS: matched via `ollama_names_match` (an untagged `model`
    must still find its own `:latest`-qualified `/api/ps` entry)."""
    from halo_harness.providers.ollama import ollama_names_match
    for entry in (ps or {}).get("models") or []:
        if isinstance(entry, dict) and (ollama_names_match(entry.get("model"), model)
                                         or ollama_names_match(entry.get("name"), model)):
            return entry
    return None


def run_calibration(host, model: str, *, start_ctx: int, min_ctx: int = MIN_CALIBRATE_CTX,
                     keep_alive: Optional[str] = None, timeout: float = 60.0,
                     step_up: bool = False, trained_context: Optional[int] = None,
                     hard_cap: Optional[int] = None) -> CalibrationResult:
    """The brief's own stepping loop (item 2): load at a candidate
    `num_ctx` (floored to a power of two, never below `min_ctx`), read
    `/api/ps` back, and step DOWN by powers of two until `size_vram >=
    size` (fully resident) or `candidate` drops below `min_ctx` (4096 by
    default, "stops at 4096 and reports 'does not fit'"). Never a real
    streamed generation and never more than one `/api/chat` call per
    step. A step whose `/api/ps` entry disappears entirely (the load
    itself failed, or something evicted it before this read) counts as
    "not fully resident" and also steps down -- this function never
    raises on a reachability failure, it just reports `does_not_fit` once
    the floor is reached with nothing better measured. Hermetic tests
    point `host.url` at a `tests/helpers/mock_ollama.MockUpstream`
    scripted with a `ps_response` sequence (see that helper's own
    `ScriptedByCallCount`-style patterns) -- never a real model.

    Round 5b part 2 (brief item 7, "part 1 follow-up"): `step_up` defaults
    False here (this bare function's own pre-existing contract, round 5b
    part 1 -- unchanged for any caller that doesn't ask for the new
    behavior explicitly) -- `halo ollama calibrate` (`ollama_cli.py`) and
    the automatic first-use trigger (`run_auto_calibration` below) are the
    two callers that explicitly pass `step_up=True` (the former unless
    `--no-up`), per the brief's own "also steps up" ask. When True, once
    the DOWN loop above finds a fitting candidate, `step_up` keeps
    doubling from there -- a FIRST guess that already fits fully resident
    (the common case on a roomy card) used to be recorded as-is even when
    the model would ALSO fit at a much larger context; stepping up finds
    the true ceiling
    instead of settling for the first lucky guess. Bounded by `min(
    trained_context, hard_cap)` when `trained_context` is given (never
    worth probing past the model's own trained window) and always by
    `hard_cap` (`providers.ollama.HARD_CONTEXT_CAP`, 131072, when
    `hard_cap` is omitted) -- stops at the FIRST non-resident load up
    here (never steps down again to search for a smaller gap), recording
    the LAST value that was still fully resident."""
    from halo_harness.providers.ollama import HARD_CONTEXT_CAP, fetch_ps
    hard_cap = hard_cap if isinstance(hard_cap, int) and hard_cap > 0 else HARD_CONTEXT_CAP
    up_bound = min(trained_context, hard_cap) if isinstance(trained_context, int) and trained_context > 0 \
        else hard_cap
    candidate = max(_floor_pow2(max(1, start_ctx)), min_ctx)
    steps = 0
    last_size: Optional[int] = None
    last_size_vram: Optional[int] = None

    def _load_and_check(ctx: int) -> "Optional[tuple[int, int]]":
        nonlocal steps
        steps += 1
        _load_model_at_num_ctx(host, model, num_ctx=ctx, keep_alive=keep_alive, timeout=timeout)
        entry = _ps_entry_for_model(fetch_ps(host, timeout=timeout), model)
        if entry is None:
            return None
        size, size_vram = entry.get("size"), entry.get("size_vram")
        if not (isinstance(size, int) and isinstance(size_vram, int) and size > 0):
            return None
        return size, size_vram

    while candidate >= min_ctx:
        result = _load_and_check(candidate)
        if result is not None:
            size, size_vram = result
            last_size, last_size_vram = size, size_vram
            if size_vram >= size:
                fitting = candidate
                if step_up:
                    up_candidate = fitting * 2
                    while up_candidate <= up_bound:
                        up_result = _load_and_check(up_candidate)
                        if up_result is None or up_result[1] < up_result[0]:
                            break
                        fitting = up_candidate
                        last_size, last_size_vram = up_result
                        up_candidate *= 2
                return CalibrationResult(outcome="fits", max_full_gpu_ctx=fitting, steps=steps,
                                          last_size=last_size, last_size_vram=last_size_vram)
        candidate //= 2
    return CalibrationResult(outcome="does_not_fit", max_full_gpu_ctx=None, steps=steps,
                              last_size=last_size, last_size_vram=last_size_vram)


def run_auto_calibration(host, model: str, *, state_dir) -> Optional[str]:
    """Brief item 2: "run it automatically the first time a model is used
    on a host with no learned cap" -- called by `agent/loop.py` before
    the first request to a new (host, model) pair this PROCESS has seen
    (the caller's own `has_calibration_entry` gate keeps this from
    re-running across different processes once an entry exists at all).
    Resolves its own starting candidate (a local/ssh GPU-based fit
    estimate when one exists, else 32768 -- the SAME "nothing known"
    default `resolve_num_ctx_and_source` substitutes for a remote host),
    runs `run_calibration`, records the result EITHER WAY (a
    `does_not_fit` outcome is remembered too), and returns ONE plain
    notice line for the caller to surface -- or `None` on any failure
    (never raises; a failed attempt degrades to "use the live fit
    estimate/remote default, same as before this existed")."""
    from halo_harness.providers.ollama import get_catalog, probe_version, trained_context_for
    from halo_harness.providers.ollama_hw import catalog_row, estimate_fit_for_host, is_local_host
    try:
        catalog = get_catalog(host)
        row = catalog_row(catalog, model)
        if row is None:
            return None
        digest = row.get("digest")
        version_info = probe_version(host)
        ollama_version = (version_info or {}).get("version") if isinstance(version_info, dict) else None
        start = estimate_fit_for_host(host, model, catalog)
        if not isinstance(start, int) or isinstance(start, bool) or start <= 0:
            start = 32768
        # Round 5b part 2: auto-calibrate also steps UP (brief item 7) --
        # bounded by this model's own trained context, so a first guess
        # that fits is still checked for a LARGER fitting value instead of
        # settling for it, same as the explicit `halo ollama calibrate`.
        trained = trained_context_for(catalog, model)
        result = run_calibration(host, model, start_ctx=start, keep_alive=host.keep_alive, trained_context=trained,
                                  step_up=True)
        record_calibration(state_dir, host_url=host.url, model=model, digest=digest,
                            max_full_gpu_ctx=result.max_full_gpu_ctx, ollama_version=ollama_version)
        where = "locally" if is_local_host(host) else "over the network"
        if result.outcome == "fits":
            return (f"Halo calibrated {model} on '{host.name}' {where}: fully resident at "
                    f"num_ctx={result.max_full_gpu_ctx} ({result.steps} step(s)). Recorded for next time "
                    f"(`halo ollama calibrate {model} --host {host.name}` to redo).")
        return (f"Halo tried to calibrate {model} on '{host.name}' {where}: it does not fit fully in GPU "
                f"memory even at num_ctx={MIN_CALIBRATE_CTX} ({result.steps} step(s)) -- using the live fit "
                f"estimate/remote default instead.")
    except Exception:
        log.debug("ollama: auto-calibration failed for %s@%s", model, host.name, exc_info=True)
        return None


def record_last_turn_throughput(state_dir, *, host_url: str, model: str, tokens_per_second: "Optional[float]",
                                 prefill_seconds: "Optional[float]", offloaded: "Optional[bool]",
                                 output_tokens: "Optional[int]" = None) -> None:
    """Brief item 7: "`halo ollama` prints the last turn's numbers per
    host" -- persisted (not just kept in the live process's own memory) so
    a SEPARATE `halo ollama` CLI invocation, always a fresh process, can
    read back what the last real TUI/print-mode turn measured. Overwritten
    every turn (one record per (host_url, model), no history kept)."""
    store = load_fit_store(state_dir)
    store["last_turns"][f"{host_url}|{model}"] = {
        "tokens_per_second": tokens_per_second, "prefill_seconds": prefill_seconds,
        "offloaded": offloaded, "output_tokens": output_tokens, "at": time.time(),
    }
    save_fit_store(state_dir, store)


def get_last_turn_throughput(state_dir, *, host_url: str, model: str) -> Optional[dict]:
    entry = load_fit_store(state_dir)["last_turns"].get(f"{host_url}|{model}")
    return entry if isinstance(entry, dict) else None

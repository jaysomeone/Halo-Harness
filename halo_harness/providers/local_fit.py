"""halo_harness.providers.local_fit -- Halo 2.0.3 round 5c (brief item 2):
"read the file, not the server, for the fit" -- wires `providers.
gguf_header`/`providers.safetensors_config`'s own `model_info` dicts into
round 3's EXISTING `providers.ollama_fit.kv_bytes_per_token`/`fit_estimate`
arithmetic (no new formula, per the brief) and round 3/5b's OS GPU-memory
read (`providers.ollama_hw`), so a file on disk gets the identical fitted-
context treatment an Ollama catalog row already gets, before anything is
ever loaded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("bridge")


def trained_context_from_model_info(model_info: "Optional[dict]") -> Optional[int]:
    """The first key ending in `.context_length` in an already-built
    `model_info` dict -- the same suffix-match idiom `providers.ollama.
    trained_context_for`/`providers.ollama_fit._model_info_value` use,
    reimplemented here (never imported from either -- `ollama_fit` takes
    no I/O-module imports by design, and `providers.ollama`'s version reads
    a live CATALOG row, not a bare dict) so this module has no dependency
    on either one beyond `ollama_fit`'s own pure arithmetic below."""
    if not isinstance(model_info, dict):
        return None
    for key, value in model_info.items():
        if key.endswith(".context_length") and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


@dataclass(frozen=True)
class FileFitResult:
    model_info: dict
    trained_context: Optional[int]
    quant_name: Optional[str]
    weight_bytes: Optional[int]
    fit_estimate: object  # Optional[int] | providers.ollama_fit.WEIGHTS_DO_NOT_FIT | None


def read_fit_inputs_for_path(path, *, fmt: str) -> "tuple[dict, Optional[str], Optional[int]]":
    """`(model_info, quant_name, weight_bytes)` for one discovered file/
    folder, dispatched on `fmt` (`"gguf"` | `"safetensors"` | `"mlx"` --
    the last two read identically, `config.json` beside `*.safetensors`;
    `"mlx"` is a label for WHICH RUNTIME serves it, never a different
    on-disk shape to parse -- see `providers.local_model_dirs`'s own
    docstring). `weight_bytes` is the on-disk size of the WEIGHT file(s)
    only (the single `.gguf` file; every `*.safetensors` file in the
    folder, never `config.json`/tokenizer files) -- the same "on-disk
    quantized size is the resident-VRAM proxy" principle `providers.
    ollama_hw.estimate_fit_for_host`'s own docstring already uses for a
    catalog row's `size` field. Never raises -- any read failure degrades
    to `({}, None, None)`."""
    try:
        if fmt == "gguf":
            from halo_harness.providers.gguf_header import gguf_model_info_for_file
            model_info, quant_name = gguf_model_info_for_file(path)
            weight_bytes = Path(path).stat().st_size
            return model_info or {}, quant_name, weight_bytes
        from halo_harness.providers.safetensors_config import safetensors_model_info_for_folder
        model_info = safetensors_model_info_for_folder(path) or {}
        weight_bytes = sum(p.stat().st_size for p in Path(path).glob("*.safetensors") if p.is_file())
        return model_info, None, (weight_bytes or None)
    except OSError:
        log.debug("local_fit: read_fit_inputs_for_path failed for %r (%s)", path, fmt, exc_info=True)
        return {}, None, None


def fit_estimate_for_file(model_info: dict, *, weight_bytes: Optional[int], hw_runner=None):
    """Round 3/5b's fit-estimate formula, unchanged, fed from a FILE's own
    `model_info` and on-disk weight size instead of an Ollama catalog row
    -- `None` when the local GPU/unified-memory read itself is unavailable
    (no supported OS tool, or `BRIDGE_TEST_NO_BACKGROUND_NET`), else
    whatever `providers.ollama_fit.fit_estimate`/`multi_gpu_fit_estimate`
    returns (a positive power-of-two int, `None`, or `WEIGHTS_DO_NOT_FIT`
    -- see that module's own docstrings)."""
    from halo_harness.providers.ollama_fit import fit_estimate, kv_bytes_per_token, multi_gpu_fit_estimate
    if not isinstance(weight_bytes, int) or isinstance(weight_bytes, bool) or weight_bytes <= 0:
        return None
    kv = kv_bytes_per_token(model_info)
    if kv is None:
        return None
    from halo_harness.providers.ollama_hw import get_local_gpu_memories
    cards = get_local_gpu_memories(runner=hw_runner)
    free = [c.free_bytes for c in cards if c is not None and isinstance(c.free_bytes, int)]
    if not free:
        return None
    if len(free) > 1:
        return multi_gpu_fit_estimate(kv_bytes_per_token=kv, free_bytes_per_card=free,
                                       resident_weight_bytes=weight_bytes)
    return fit_estimate(kv_bytes_per_token=kv, free_memory_bytes=free[0], resident_weight_bytes=weight_bytes)


def fit_result_for_path(path, *, fmt: str, hw_runner=None) -> FileFitResult:
    """ONE entry point `providers.local_models`/`local_cli.py` call for a
    discovered file/folder's full fit picture -- reads the file, builds
    `model_info`, and computes the live fit estimate, all in one call so
    neither caller repeats the dispatch-on-`fmt` logic above."""
    model_info, quant_name, weight_bytes = read_fit_inputs_for_path(path, fmt=fmt)
    trained_context = trained_context_from_model_info(model_info)
    fit = fit_estimate_for_file(model_info, weight_bytes=weight_bytes, hw_runner=hw_runner) if model_info else None
    return FileFitResult(model_info=model_info, trained_context=trained_context, quant_name=quant_name,
                          weight_bytes=weight_bytes, fit_estimate=fit)

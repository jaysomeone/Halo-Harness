"""tests.test_local_fit -- Halo 2.0.3 round 5c: the GGUF/safetensors ->
fit_estimate glue (providers.local_fit), against an INJECTED GPU-memory
runner (never a real nvidia-smi) -- same test seam tests/test_ollama_hw_5b.py
already uses for providers.ollama_hw.
"""
from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

# 24 GiB total, 2 GiB used, 22 GiB free, one card -- plenty of room for a
# tiny fixture "model" so the fit estimate resolves to a real, large power
# of two rather than WEIGHTS_DO_NOT_FIT.
_ONE_CARD_22GIB_FREE_CSV = "24576, 2048, 22528, Mock GPU"


def _reset():
    from halo_harness.providers import ollama_hw
    ollama_hw.reset_local_gpu_cache()


def _write_gguf(path: Path) -> None:
    def enc_str(s):
        b = s.encode("utf-8")
        return struct.pack("<Q", len(b)) + b

    def enc_kv(key, vtype, value):
        out = enc_str(key) + struct.pack("<I", vtype)
        return out + (struct.pack("<I", value) if vtype == 4 else enc_str(value))

    kvs = [enc_kv("general.architecture", 8, "qwen3"), enc_kv("general.file_type", 4, 2),
           enc_kv("qwen3.context_length", 4, 40960), enc_kv("qwen3.block_count", 4, 48),
           enc_kv("qwen3.attention.head_count", 4, 32), enc_kv("qwen3.attention.head_count_kv", 4, 8),
           enc_kv("qwen3.embedding_length", 4, 4096)]
    data = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(kvs)) + b"".join(kvs)
    path.write_bytes(data + b"\0" * 1024)  # pad -- weight_bytes is a stand-in for real tensor data


@test
def test_fit_estimate_for_gguf_file_reuses_ollama_fit_formula(ctx: Ctx):
    from halo_harness.providers.local_fit import fit_result_for_path
    _reset()
    d = Path(tempfile.mkdtemp(prefix="local-fit-gguf-"))
    p = d / "model.gguf"
    _write_gguf(p)
    result = fit_result_for_path(p, fmt="gguf", hw_runner=lambda argv, timeout: _ONE_CARD_22GIB_FREE_CSV)
    ctx.check(f"trained_context read from the file, got {result.trained_context}", result.trained_context == 40960)
    ctx.check(f"quant_name read from the file, got {result.quant_name!r}", result.quant_name == "Q4_0")
    ctx.check(f"weight_bytes is the file's own size, got {result.weight_bytes}",
              result.weight_bytes == p.stat().st_size)
    ctx.check(f"fit_estimate is a positive power of two, got {result.fit_estimate!r}",
              isinstance(result.fit_estimate, int) and result.fit_estimate > 0
              and (result.fit_estimate & (result.fit_estimate - 1) == 0))


@test
def test_fit_estimate_for_safetensors_folder_sums_shard_sizes(ctx: Ctx):
    from halo_harness.providers.local_fit import fit_result_for_path
    import json
    _reset()
    d = Path(tempfile.mkdtemp(prefix="local-fit-st-"))
    (d / "config.json").write_text(json.dumps({
        "model_type": "llama", "max_position_embeddings": 8192, "num_hidden_layers": 32,
        "num_attention_heads": 32, "num_key_value_heads": 8, "hidden_size": 4096,
    }), encoding="utf-8")
    (d / "model-00001-of-00002.safetensors").write_bytes(b"\0" * 1000)
    (d / "model-00002-of-00002.safetensors").write_bytes(b"\0" * 2000)
    result = fit_result_for_path(d, fmt="safetensors", hw_runner=lambda argv, timeout: _ONE_CARD_22GIB_FREE_CSV)
    ctx.check(f"weight_bytes sums every shard, got {result.weight_bytes}", result.weight_bytes == 3000)
    ctx.check(f"trained_context from config.json, got {result.trained_context}", result.trained_context == 8192)


@test
def test_no_gpu_memory_available_yields_none_fit_without_raising(ctx: Ctx):
    from halo_harness.providers.local_fit import fit_result_for_path
    _reset()
    d = Path(tempfile.mkdtemp(prefix="local-fit-nogpu-"))
    p = d / "model.gguf"
    _write_gguf(p)
    result = fit_result_for_path(p, fmt="gguf", hw_runner=lambda argv, timeout: None)
    ctx.check(f"no GPU read -> fit_estimate is None, got {result.fit_estimate!r}", result.fit_estimate is None)
    ctx.check(f"model_info was still read from the file, got {result.trained_context}", result.trained_context == 40960)


@test
def test_unreadable_file_degrades_to_no_fit_input(ctx: Ctx):
    from halo_harness.providers.local_fit import fit_result_for_path
    _reset()
    d = Path(tempfile.mkdtemp(prefix="local-fit-bad-"))
    p = d / "not-really.gguf"
    p.write_bytes(b"NOPE")
    result = fit_result_for_path(p, fmt="gguf", hw_runner=lambda argv, timeout: _ONE_CARD_22GIB_FREE_CSV)
    ctx.check("bad magic -> no model_info", result.model_info == {})
    ctx.check("bad magic -> no trained_context", result.trained_context is None)
    ctx.check("bad magic -> no fit_estimate (never guesses)", result.fit_estimate is None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

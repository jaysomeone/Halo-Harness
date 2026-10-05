"""tests.test_safetensors_config -- Halo 2.0.3 round 5c: the transformers
`config.json` reader against fixture folders only.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _folder(config: "dict | None", *, write_safetensors: bool = True) -> Path:
    d = Path(tempfile.mkdtemp(prefix="st-config-"))
    if config is not None:
        (d / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if write_safetensors:
        (d / "model.safetensors").write_bytes(b"\0" * 4096)
    return d


_LLAMA_LIKE = {
    "model_type": "llama", "max_position_embeddings": 8192, "num_hidden_layers": 32,
    "num_attention_heads": 32, "num_key_value_heads": 8, "hidden_size": 4096,
}


@test
def test_full_config_builds_model_info(ctx: Ctx):
    from halo_harness.providers.safetensors_config import safetensors_model_info_for_folder
    info = safetensors_model_info_for_folder(_folder(_LLAMA_LIKE))
    ctx.check(f"architecture is model_type, got {info}", info.get("general.architecture") == "llama")
    ctx.check(f"context_length mapped, got {info}", info.get("llama.context_length") == 8192)
    ctx.check(f"block_count mapped, got {info}", info.get("llama.block_count") == 32)
    ctx.check(f"head_count_kv mapped, got {info}", info.get("llama.attention.head_count_kv") == 8)
    ctx.check(f"head_count mapped, got {info}", info.get("llama.attention.head_count") == 32)
    ctx.check(f"embedding_length mapped, got {info}", info.get("llama.embedding_length") == 4096)


@test
def test_missing_num_key_value_heads_falls_back_to_head_count(ctx: Ctx):
    """No GQA declared -- num_key_value_heads defaults to num_attention_heads
    (this module's own documented fallback, see its docstring)."""
    from halo_harness.providers.safetensors_config import safetensors_model_info_for_folder
    config = dict(_LLAMA_LIKE)
    del config["num_key_value_heads"]
    info = safetensors_model_info_for_folder(_folder(config))
    ctx.check(f"head_count_kv falls back to head_count, got {info}", info.get("llama.attention.head_count_kv") == 32)


@test
def test_architectures_list_used_when_model_type_absent(ctx: Ctx):
    from halo_harness.providers.safetensors_config import safetensors_model_info_for_folder
    config = {k: v for k, v in _LLAMA_LIKE.items() if k != "model_type"}
    config["architectures"] = ["LlamaForCausalLM"]
    info = safetensors_model_info_for_folder(_folder(config))
    ctx.check(f"architecture lowercased from architectures[0], got {info}",
              info.get("general.architecture") == "llamaforcausallm")
    ctx.check(f"context_length still mapped under it, got {info}", info.get("llamaforcausallm.context_length") == 8192)


@test
def test_feeds_kv_bytes_per_token_unchanged(ctx: Ctx):
    from halo_harness.providers.safetensors_config import safetensors_model_info_for_folder
    from halo_harness.providers.ollama_fit import kv_bytes_per_token
    info = safetensors_model_info_for_folder(_folder(_LLAMA_LIKE))
    expected = 2.0 * 32 * 8 * (4096 / 32) * 2.0
    ctx.check(f"kv_bytes_per_token matches, got {kv_bytes_per_token(info)} want {expected}",
              kv_bytes_per_token(info) == expected)


@test
def test_missing_config_json_returns_none(ctx: Ctx):
    from halo_harness.providers.safetensors_config import safetensors_model_info_for_folder
    ctx.check("no config.json -> None", safetensors_model_info_for_folder(_folder(None)) is None)


@test
def test_malformed_json_returns_none(ctx: Ctx):
    from halo_harness.providers.safetensors_config import safetensors_model_info_for_folder
    d = _folder(None)
    (d / "config.json").write_text("{not json", encoding="utf-8")
    ctx.check("malformed config.json -> None", safetensors_model_info_for_folder(d) is None)


@test
def test_config_with_none_of_the_wanted_fields_returns_empty_dict(ctx: Ctx):
    from halo_harness.providers.safetensors_config import safetensors_model_info_for_folder
    info = safetensors_model_info_for_folder(_folder({"model_type": "llama", "vocab_size": 32000}))
    ctx.check(f"no usable field -> {{}}, got {info}", info == {})


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

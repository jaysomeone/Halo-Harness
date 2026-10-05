"""halo_harness.providers.safetensors_config -- Halo 2.0.3 round 5c (brief
item 2): reads a transformers-style model folder's own `config.json`
(sitting beside its `*.safetensors` weight files) for the SAME fields a
GGUF header carries -- `max_position_embeddings`, `num_hidden_layers`,
`num_key_value_heads`, `hidden_size`, `num_attention_heads` -- never loads
any `*.safetensors` file itself (brief: "never load a model to learn
this"; `config.json` is the one small JSON file every such folder already
has, a few KB regardless of the model's own size).

`num_key_value_heads` missing from `config.json` means "no GQA -- same as
`num_attention_heads`", the documented default every major transformers
architecture config (Llama, Mistral, Qwen2, ...) uses for this exact field
-- general knowledge of the transformers config convention, not sourced
from either of this round's two fetched research docs (docs/harness/
LOCAL-MODELS-RESEARCH.md section 8 flags the context-length field name
itself as UNCONFIRMED at the per-architecture level and does not discuss
this fallback at all). Backfilling it HERE (rather than leaving it to
`providers.ollama_fit.kv_bytes_per_token`, which has no such fallback --
it returns `None` outright when `.attention.head_count_kv` is missing) is
the one place this module's output differs from a bare pass-through of
`config.json`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_ARCH_FALLBACK = "model"


def read_safetensors_config(folder) -> Optional[dict]:
    """The raw, parsed `config.json` beside this folder's `*.safetensors`
    files -- `None` when the file is missing, unreadable, not a JSON
    object, or the folder doesn't exist at all. Never raises."""
    path = Path(folder) / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _architecture_name(config: dict) -> str:
    """`model_type` (the field every transformers `config.json` carries,
    e.g. `"llama"`/`"qwen2"`/`"mistral"`) else the first entry of
    `architectures` (e.g. `"LlamaForCausalLM"`, lowercased) else the
    generic fallback -- mirrors how a GGUF's own `general.architecture`
    string is used as the `<arch>.*` key prefix, so both readers build the
    identically-shaped `model_info` dict."""
    model_type = config.get("model_type")
    if isinstance(model_type, str) and model_type.strip():
        return model_type.strip().lower()
    architectures = config.get("architectures")
    if isinstance(architectures, list) and architectures and isinstance(architectures[0], str):
        return architectures[0].strip().lower() or _ARCH_FALLBACK
    return _ARCH_FALLBACK


def _positive_int(config: dict, key: str) -> Optional[int]:
    value = config.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def safetensors_model_info(config: dict) -> dict:
    """The same `model_info`-shaped dict `providers.gguf_header.
    gguf_model_info` builds (`general.architecture` plus this
    architecture's `.context_length`/`.block_count`/
    `.attention.head_count_kv`/`.attention.head_count`/
    `.embedding_length`), from an already-parsed `config.json`. `{}` when
    none of `max_position_embeddings`/`num_hidden_layers`/`hidden_size`/
    `num_attention_heads` are present at all (not a transformers-shaped
    config this module recognizes)."""
    context_length = _positive_int(config, "max_position_embeddings")
    block_count = _positive_int(config, "num_hidden_layers")
    embedding_length = _positive_int(config, "hidden_size")
    head_count = _positive_int(config, "num_attention_heads")
    # The one fallback this module adds beyond a bare field rename -- see
    # this module's own docstring.
    head_count_kv = _positive_int(config, "num_key_value_heads") or head_count
    if context_length is None and block_count is None and embedding_length is None and head_count is None:
        return {}
    arch = _architecture_name(config)
    out = {"general.architecture": arch}
    if context_length is not None:
        out[f"{arch}.context_length"] = context_length
    if block_count is not None:
        out[f"{arch}.block_count"] = block_count
    if head_count_kv is not None:
        out[f"{arch}.attention.head_count_kv"] = head_count_kv
    if head_count is not None:
        out[f"{arch}.attention.head_count"] = head_count
    if embedding_length is not None:
        out[f"{arch}.embedding_length"] = embedding_length
    return out


def safetensors_model_info_for_folder(folder) -> Optional[dict]:
    """Best-effort `model_info` for the folder at `folder` -- `None` when
    `config.json` is missing/unreadable/not an object; `{}` (not `None`)
    when it parsed but carries none of the fields this module reads (a
    caller that wants "nothing learned" to mean one thing, not two, should
    treat both as "no fit input" -- `providers.local_fit` does)."""
    config = read_safetensors_config(folder)
    if config is None:
        return None
    return safetensors_model_info(config)

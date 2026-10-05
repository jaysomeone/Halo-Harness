"""halo_harness.providers.gguf_header -- Halo 2.0.3 round 5c (brief item 2):
a minimal GGUF HEADER reader -- magic, version, and the key-value metadata
block only. Never reads the tensor-info table or any tensor data that
follows metadata in the file (brief: "never load a model to learn this"):
the metadata block is a few KB to a few hundred KB even on a huge model, so
this stays cheap regardless of the file's own total size.

GGUF layout (the format itself, not any one fetched page -- the two
research docs this round read (docs/harness/GPU-RESEARCH.md,
docs/harness/LOCAL-MODELS-RESEARCH.md) describe Ollama's/llama.cpp's
BEHAVIOUR around GGUF files, not the byte layout; the binary shape below is
the long-stable, publicly documented GGUF spec `ggml-org/llama.cpp` and
`ggml-org/ggml` both implement, carried over from general knowledge of the
format rather than a URL either doc cites):

    magic            4 bytes, literal b"GGUF"
    version          uint32 (this reader supports >= 2; v1 used uint32
                      counts instead of uint64 and is obsolete/unseen today)
    tensor_count     uint64 (read and kept, never used for anything here)
    metadata_kv_count uint64
    metadata_kv_count * {
        key           gguf string: uint64 length + utf-8 bytes
        value_type    uint32 (GGUFValueType below)
        value         per value_type
    }

GGUF's OWN metadata keys already use the exact `<arch>.context_length` /
`<arch>.block_count` / `<arch>.attention.head_count_kv` /
`<arch>.embedding_length` / `<arch>.attention.head_count` naming
`providers.ollama`'s `/api/show`-derived `model_info` dict uses (Ollama
surfaces these keys close to verbatim) -- so `gguf_model_info` below is a
filter, not a translation, and the result plugs straight into
`providers.ollama_fit.kv_bytes_per_token` with no new formula.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

GGUF_MAGIC = b"GGUF"
_MAX_TOP_LEVEL_STRING_BYTES = 1 << 20  # 1 MiB -- architecture/name strings are always tiny; a bigger one is refused

# GGUFValueType (the format's own enum; stable across llama.cpp's history).
_U8, _I8, _U16, _I16, _U32, _I32, _F32, _BOOL, _STRING, _ARRAY, _U64, _I64, _F64 = range(13)

_SCALAR_STRUCT = {
    _U8: "<B", _I8: "<b", _U16: "<H", _I16: "<h", _U32: "<I", _I32: "<i", _F32: "<f",
    _BOOL: "<B", _U64: "<Q", _I64: "<q", _F64: "<d",
}

# llama.cpp's public `enum llama_ftype` (llama.h) -- general knowledge of a
# long-stable public header, not sourced from either of this round's two
# fetched research docs (neither discusses `general.file_type`'s int->name
# mapping at all). An unrecognized value never raises -- `f"unknown({n})"`.
_FILE_TYPE_NAMES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1",
    10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M",
    16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S",
    22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M",
    28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16", 34: "TQ1_0", 35: "TQ2_0",
}


class GGUFParseError(Exception):
    """Bad magic, an unsupported version, or a truncated/malformed metadata
    block. Never raised for a well-formed header that simply lacks a key
    this module wants -- that degrades to a missing dict entry (same
    "never guess" discipline as the rest of `providers/`), never an
    exception."""


@dataclass(frozen=True)
class GGUFHeader:
    version: int
    tensor_count: int
    metadata: dict = field(default_factory=dict)


def _read_exact(f, n: int) -> bytes:
    data = f.read(n)
    if len(data) != n:
        raise GGUFParseError(f"truncated GGUF file (wanted {n} bytes, got {len(data)})")
    return data


def _read_scalar(f, vtype: int):
    fmt = _SCALAR_STRUCT[vtype]
    value = struct.unpack(fmt, _read_exact(f, struct.calcsize(fmt)))[0]
    return bool(value) if vtype == _BOOL else value


def _read_u32(f) -> int:
    return _read_scalar(f, _U32)


def _read_u64(f) -> int:
    return _read_scalar(f, _U64)


def _read_gguf_string(f, *, max_bytes: Optional[int] = None) -> str:
    n = _read_u64(f)
    if max_bytes is not None and n > max_bytes:
        raise GGUFParseError(f"implausible string length {n} (max {max_bytes})")
    return _read_exact(f, n).decode("utf-8", errors="replace")


def _skip_exact(f, n: int, file_size: int) -> None:
    if n < 0:
        raise GGUFParseError("truncated GGUF file (negative-length value)")
    f.seek(n, 1)
    if f.tell() > file_size:
        raise GGUFParseError("truncated GGUF file (a value runs past end of file)")


def _skip_value(f, vtype: int, file_size: int) -> None:
    """Discards one metadata VALUE without materializing it -- the only
    path a huge array (a 100k+-entry tokenizer vocabulary is the common
    case) ever takes, by seeking rather than reading, so this stays cheap
    regardless of vocabulary size."""
    if vtype == _STRING:
        _skip_exact(f, _read_u64(f), file_size)
        return
    if vtype == _ARRAY:
        elem_type = _read_u32(f)
        count = _read_u64(f)
        if elem_type == _STRING:
            for _ in range(count):
                _skip_exact(f, _read_u64(f), file_size)
        elif elem_type == _ARRAY:
            for _ in range(count):
                _skip_value(f, _ARRAY, file_size)
        elif elem_type in _SCALAR_STRUCT:
            _skip_exact(f, struct.calcsize(_SCALAR_STRUCT[elem_type]) * count, file_size)
        else:
            raise GGUFParseError(f"unknown array element type {elem_type}")
        return
    if vtype in _SCALAR_STRUCT:
        _skip_exact(f, struct.calcsize(_SCALAR_STRUCT[vtype]), file_size)
        return
    raise GGUFParseError(f"unknown value type {vtype}")


def parse_gguf_header(path) -> GGUFHeader:
    """Raises `GGUFParseError` on bad magic, an unsupported version, or a
    truncated/malformed metadata block -- callers that want a `None`
    instead (the discovery/listing path, never a test pinning the error
    itself) use `gguf_model_info_for_file` below."""
    path = Path(path)
    file_size = path.stat().st_size
    with open(path, "rb") as f:
        magic = _read_exact(f, 4)
        if magic != GGUF_MAGIC:
            raise GGUFParseError(f"not a GGUF file (bad magic {magic!r})")
        version = _read_u32(f)
        if version < 2:
            raise GGUFParseError(f"unsupported GGUF version {version} (only >= 2 is supported)")
        tensor_count = _read_u64(f)
        kv_count = _read_u64(f)
        metadata: dict = {}
        for _ in range(kv_count):
            key = _read_gguf_string(f, max_bytes=_MAX_TOP_LEVEL_STRING_BYTES)
            vtype = _read_u32(f)
            if vtype == _STRING:
                metadata[key] = _read_gguf_string(f, max_bytes=_MAX_TOP_LEVEL_STRING_BYTES)
            elif vtype == _ARRAY:
                _skip_value(f, vtype, file_size)  # never need an array value for the fit arithmetic
            elif vtype in _SCALAR_STRUCT:
                metadata[key] = _read_scalar(f, vtype)
            else:
                raise GGUFParseError(f"unknown value type {vtype} for key {key!r}")
    return GGUFHeader(version=version, tensor_count=tensor_count, metadata=metadata)


def gguf_model_info(metadata: dict) -> dict:
    """The `model_info`-shaped subset of `metadata` (`general.architecture`
    plus this architecture's own `.context_length`/`.block_count`/
    `.attention.head_count_kv`/`.attention.head_count`/`.embedding_length`)
    -- GGUF's OWN key naming already matches `providers.ollama.get_catalog`'s
    `/api/show`-derived shape, so this is a filter, not a translation, and
    feeds `providers.ollama_fit.kv_bytes_per_token` unchanged. `{}` when no
    `general.architecture` string is present at all."""
    arch = metadata.get("general.architecture")
    if not isinstance(arch, str) or not arch:
        return {}
    out = {"general.architecture": arch}
    for suffix in (".context_length", ".block_count", ".attention.head_count_kv",
                   ".attention.head_count", ".embedding_length"):
        key = f"{arch}{suffix}"
        if key in metadata:
            out[key] = metadata[key]
    return out


def gguf_quant_name(metadata: dict) -> Optional[str]:
    """`general.file_type`'s int value resolved through llama.cpp's public
    `llama_ftype` enum (this module's own `_FILE_TYPE_NAMES`) -- `None`
    when the key is absent, `f"unknown({n})"` for a value this table
    doesn't recognize (never raises on an unexpected/future quant type)."""
    value = metadata.get("general.file_type")
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return _FILE_TYPE_NAMES.get(value, f"unknown({value})")


def gguf_model_info_for_file(path) -> "tuple[Optional[dict], Optional[str]]":
    """Best-effort `(model_info, quant_name)` for `path` -- `(None, None)`
    on ANY parse failure (bad magic, truncated, unreadable) rather than
    raising; this is the function the discovery/listing path
    (`providers.local_fit`) calls, never `parse_gguf_header` directly."""
    try:
        header = parse_gguf_header(path)
    except (GGUFParseError, OSError):
        return None, None
    return gguf_model_info(header.metadata), gguf_quant_name(header.metadata)

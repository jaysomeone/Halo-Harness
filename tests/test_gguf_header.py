"""tests.test_gguf_header -- Halo 2.0.3 round 5c: the minimal GGUF header
reader against tiny HAND-BUILT fixture files (a few hundred bytes each),
never a real model. `_build_gguf` below is this test file's own encoder --
deliberately independent of `halo_harness.providers.gguf_header`'s own
decoder (copying its logic here would let a shared bug cancel out).
"""
from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_U32, _U64, _F32, _BOOL, _STRING, _ARRAY = 4, 10, 6, 7, 8, 9  # GGUFValueType subset this file needs


def _enc_string(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<Q", len(b)) + b


def _enc_kv(key: str, vtype: int, value) -> bytes:
    out = _enc_string(key) + struct.pack("<I", vtype)
    if vtype == _U32:
        out += struct.pack("<I", value)
    elif vtype == _U64:
        out += struct.pack("<Q", value)
    elif vtype == _F32:
        out += struct.pack("<f", value)
    elif vtype == _BOOL:
        out += struct.pack("<B", 1 if value else 0)
    elif vtype == _STRING:
        out += _enc_string(value)
    else:
        raise ValueError(f"unsupported test vtype {vtype}")
    return out


def _enc_string_array_kv(key: str, values: "list[str]") -> bytes:
    """A `tokenizer.ggml.tokens`-shaped entry -- exercises the array-skip
    path (the thing a real vocabulary stresses, here with 3 tiny strings
    instead of 100k+)."""
    out = _enc_string(key) + struct.pack("<I", _ARRAY) + struct.pack("<I", _STRING) + struct.pack("<Q", len(values))
    for v in values:
        out += _enc_string(v)
    return out


def _build_gguf(kvs: "list[bytes]", *, version: int = 3, tensor_count: int = 0, magic: bytes = b"GGUF") -> bytes:
    return magic + struct.pack("<I", version) + struct.pack("<Q", tensor_count) + struct.pack("<Q", len(kvs)) \
        + b"".join(kvs)


def _write(data: bytes) -> Path:
    d = Path(tempfile.mkdtemp(prefix="gguf-fixture-"))
    p = d / "model.gguf"
    p.write_bytes(data)
    return p


def _qwen3_like_bytes() -> bytes:
    return _build_gguf([
        _enc_kv("general.architecture", _STRING, "qwen3"),
        _enc_kv("general.name", _STRING, "qwen3-test"),
        _enc_kv("general.file_type", _U32, 2),  # Q4_0
        _enc_kv("qwen3.context_length", _U32, 40960),
        _enc_kv("qwen3.block_count", _U32, 48),
        _enc_kv("qwen3.attention.head_count", _U32, 32),
        _enc_kv("qwen3.attention.head_count_kv", _U32, 8),
        _enc_kv("qwen3.embedding_length", _U32, 4096),
        _enc_string_array_kv("tokenizer.ggml.tokens", ["<s>", "</s>", "hi"]),
    ])


@test
def test_valid_header_extracts_model_info_and_quant(ctx: Ctx):
    from halo_harness.providers.gguf_header import gguf_model_info, gguf_quant_name, parse_gguf_header
    path = _write(_qwen3_like_bytes())
    header = parse_gguf_header(path)
    ctx.check(f"version 3, got {header.version}", header.version == 3)
    ctx.check(f"architecture read, got {header.metadata.get('general.architecture')!r}",
              header.metadata.get("general.architecture") == "qwen3")
    ctx.check("array-skip left the tokenizer key out of metadata (never materialized)",
              "tokenizer.ggml.tokens" not in header.metadata)
    info = gguf_model_info(header.metadata)
    ctx.check(f"context_length in model_info, got {info}", info.get("qwen3.context_length") == 40960)
    ctx.check(f"block_count in model_info, got {info}", info.get("qwen3.block_count") == 48)
    ctx.check(f"head_count_kv in model_info, got {info}", info.get("qwen3.attention.head_count_kv") == 8)
    ctx.check(f"embedding_length in model_info, got {info}", info.get("qwen3.embedding_length") == 4096)
    ctx.check(f"quant name resolved, got {gguf_quant_name(header.metadata)!r}",
              gguf_quant_name(header.metadata) == "Q4_0")


@test
def test_model_info_feeds_kv_bytes_per_token_unchanged(ctx: Ctx):
    """Brief item 2: "reuse the existing kv_bytes_per_token/fit_estimate
    arithmetic; no new formula" -- pinned directly against ollama_fit's
    own function, on a GGUF-derived model_info dict."""
    from halo_harness.providers.gguf_header import gguf_model_info, parse_gguf_header
    from halo_harness.providers.ollama_fit import kv_bytes_per_token
    header = parse_gguf_header(_write(_qwen3_like_bytes()))
    info = gguf_model_info(header.metadata)
    # head_dim = 4096 / 32 = 128; bytes/tok = 2 * 48 * 8 * 128 * 2.0(f16)
    expected = 2.0 * 48 * 8 * (4096 / 32) * 2.0
    got = kv_bytes_per_token(info)
    ctx.check(f"kv_bytes_per_token matches the documented formula, got {got} want {expected}", got == expected)


@test
def test_unknown_file_type_value_degrades_to_unknown_label(ctx: Ctx):
    from halo_harness.providers.gguf_header import gguf_quant_name, parse_gguf_header
    data = _build_gguf([_enc_kv("general.architecture", _STRING, "x"), _enc_kv("general.file_type", _U32, 9999)])
    header = parse_gguf_header(_write(data))
    ctx.check(f"unrecognized file_type degrades, got {gguf_quant_name(header.metadata)!r}",
              gguf_quant_name(header.metadata) == "unknown(9999)")


@test
def test_no_architecture_key_yields_empty_model_info(ctx: Ctx):
    from halo_harness.providers.gguf_header import gguf_model_info, parse_gguf_header
    header = parse_gguf_header(_write(_build_gguf([_enc_kv("general.name", _STRING, "no-arch")])))
    ctx.check("no general.architecture -> {}", gguf_model_info(header.metadata) == {})


@test
def test_wrong_magic_raises_gguf_parse_error(ctx: Ctx):
    from halo_harness.providers.gguf_header import GGUFParseError, parse_gguf_header
    bad = b"OOPS" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", 0)
    try:
        parse_gguf_header(_write(bad))
        ctx.check("bad magic must raise GGUFParseError", False)
    except GGUFParseError as e:
        ctx.check(f"error names the bad magic, got {e}", "magic" in str(e))


@test
def test_truncated_file_raises_gguf_parse_error(ctx: Ctx):
    from halo_harness.providers.gguf_header import GGUFParseError, parse_gguf_header
    full = _qwen3_like_bytes()
    truncated = full[: len(full) - 40]  # cut off mid-metadata, more keys were still expected
    try:
        parse_gguf_header(_write(truncated))
        ctx.check("a truncated file must raise GGUFParseError", False)
    except GGUFParseError as e:
        ctx.check(f"error mentions truncation, got {e}", "truncated" in str(e).lower())


@test
def test_gguf_model_info_for_file_never_raises_on_bad_input(ctx: Ctx):
    from halo_harness.providers.gguf_header import gguf_model_info_for_file
    d = Path(tempfile.mkdtemp(prefix="gguf-missing-"))
    info, quant = gguf_model_info_for_file(d / "does-not-exist.gguf")
    ctx.check("missing file -> (None, None), never raises", info is None and quant is None)
    bad = d / "bad.gguf"
    bad.write_bytes(b"NOPE")
    info2, quant2 = gguf_model_info_for_file(bad)
    ctx.check("bad magic -> (None, None), never raises", info2 is None and quant2 is None)


@test
def test_version_1_is_rejected_as_unsupported(ctx: Ctx):
    from halo_harness.providers.gguf_header import GGUFParseError, parse_gguf_header
    data = _build_gguf([], version=1)
    try:
        parse_gguf_header(_write(data))
        ctx.check("version 1 must raise GGUFParseError (uint64 counts assumed)", False)
    except GGUFParseError as e:
        ctx.check(f"error names the version, got {e}", "version" in str(e).lower())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

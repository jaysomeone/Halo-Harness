"""tests.test_local_model_dirs -- Halo 2.0.3 round 5c: the `huggingface.
model_dirs` folder scan (fixture trees only) and `/local add`/`forget`
persistence (hermetic -- scoped BRIDGE_TEST_HOME/BRIDGE_STATE_DIR, never
the real ~/.halo).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_PROVIDER_ENV_VARS = ("HF_TOKEN", "OLLAMA_HOST")


class _Env:
    """Same pattern as tests/test_providers_huggingface_local.py's own
    `_Env` -- scopes config.json to a fresh tempdir per test."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="local-model-dirs-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        for k in _PROVIDER_ENV_VARS:
            os.environ.pop(k, None)
        self.home = d
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _make_gguf(path: Path, size: int = 128) -> None:
    path.write_bytes(b"GGUF" + b"\0" * size)


def _make_safetensors_folder(folder: Path, *, extra_config: "dict | None" = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    config = {"model_type": "llama", "max_position_embeddings": 4096}
    if extra_config:
        config.update(extra_config)
    (folder / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (folder / "model.safetensors").write_bytes(b"\0" * 2048)


@test
def test_scan_finds_gguf_file_anywhere_under_the_root(ctx: Ctx):
    from halo_harness.providers.local_model_dirs import scan_model_dirs
    d = Path(tempfile.mkdtemp(prefix="scan-gguf-"))
    (d / "sub").mkdir()
    _make_gguf(d / "sub" / "tiny.gguf", size=500)
    rows = scan_model_dirs(dirs=[d])
    ctx.check(f"exactly one gguf row, got {rows}", len(rows) == 1 and rows[0].format == "gguf")
    ctx.check(f"size matches the real file, got {rows[0].size_bytes}", rows[0].size_bytes == 504)


@test
def test_scan_finds_safetensors_folder_and_stops_descending_into_it(ctx: Ctx):
    from halo_harness.providers.local_model_dirs import scan_model_dirs
    d = Path(tempfile.mkdtemp(prefix="scan-st-"))
    model_dir = d / "my-model"
    _make_safetensors_folder(model_dir)
    (model_dir / "checkpoint-1").mkdir()
    _make_gguf(model_dir / "checkpoint-1" / "decoy.gguf")  # must NOT be reported separately
    rows = scan_model_dirs(dirs=[d])
    ctx.check(f"exactly one row (the folder, not its checkpoint subdir), got {rows}", len(rows) == 1)
    ctx.check(f"format is safetensors, got {rows[0].format!r}", rows[0].format == "safetensors")
    ctx.check(f"path is the folder itself, got {rows[0].path}", rows[0].path == model_dir)


@test
def test_scan_labels_mlx_by_folder_name_heuristic(ctx: Ctx):
    from halo_harness.providers.local_model_dirs import scan_model_dirs
    d = Path(tempfile.mkdtemp(prefix="scan-mlx-name-"))
    model_dir = d / "mlx-community" / "Llama-3-8B-4bit"
    _make_safetensors_folder(model_dir)
    rows = scan_model_dirs(dirs=[d])
    ctx.check(f"labelled mlx by path component, got {rows}", len(rows) == 1 and rows[0].format == "mlx")


@test
def test_scan_labels_mlx_by_quantization_key_heuristic(ctx: Ctx):
    from halo_harness.providers.local_model_dirs import scan_model_dirs
    d = Path(tempfile.mkdtemp(prefix="scan-mlx-quant-"))
    model_dir = d / "some-plain-folder-name"
    _make_safetensors_folder(model_dir, extra_config={"quantization": {"group_size": 64, "bits": 4}})
    rows = scan_model_dirs(dirs=[d])
    ctx.check(f"labelled mlx by quantization key, got {rows}", len(rows) == 1 and rows[0].format == "mlx")


@test
def test_scan_never_descends_into_a_symlinked_subdirectory(ctx: Ctx):
    from halo_harness.providers.local_model_dirs import scan_model_dirs
    d = Path(tempfile.mkdtemp(prefix="scan-escape-"))
    outside = Path(tempfile.mkdtemp(prefix="scan-outside-"))
    _make_gguf(outside / "secret.gguf")
    link = d / "escaped-link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        ctx.check("symlinks unsupported on this host -- nothing to test, skipped cleanly", True)
        return
    _make_gguf(d / "real.gguf")
    rows = scan_model_dirs(dirs=[d])
    names = {r.path.name for r in rows}
    ctx.check(f"the symlinked-outside file is never reported, got {names}", "secret.gguf" not in names)
    ctx.check(f"the real file still is, got {names}", "real.gguf" in names)


@test
def test_scan_respects_depth_limit(ctx: Ctx):
    from halo_harness.providers.local_model_dirs import _MAX_WALK_DEPTH, scan_model_dirs
    d = Path(tempfile.mkdtemp(prefix="scan-deep-"))
    deep = d
    for i in range(_MAX_WALK_DEPTH + 3):
        deep = deep / f"lvl{i}"
    deep.mkdir(parents=True)
    _make_gguf(deep / "too-deep.gguf")
    rows = scan_model_dirs(dirs=[d])
    ctx.check(f"a file past the depth limit is never reported, got {rows}", rows == [])


@test
def test_scan_missing_root_returns_nothing(ctx: Ctx):
    from halo_harness.providers.local_model_dirs import scan_model_dirs
    ctx.check("missing root -> []", scan_model_dirs(dirs=[Path(tempfile.mkdtemp()) / "nope"]) == [])


@test
def test_add_and_forget_persist_to_config(ctx: Ctx):
    from halo_harness.providers.local_model_dirs import add_model_dir, forget_model_dir, resolve_model_dirs
    with _Env():
        folder = Path(tempfile.mkdtemp(prefix="add-folder-"))
        ok, msg = add_model_dir(str(folder))
        ctx.check(f"add succeeds, got {msg!r}", ok)
        ctx.check(f"now in resolve_model_dirs, got {resolve_model_dirs()}",
                  any(d.resolve() == folder.resolve() for d in resolve_model_dirs()))
        ok2, msg2 = add_model_dir(str(folder))
        ctx.check(f"adding the same folder twice is refused, got {msg2!r}", not ok2)
        ok3, msg3 = forget_model_dir(str(folder))
        ctx.check(f"forget succeeds, got {msg3!r}", ok3)
        ctx.check("no longer in resolve_model_dirs", resolve_model_dirs() == [])
        ok4, msg4 = forget_model_dir(str(folder))
        ctx.check(f"forgetting an absent entry is refused, got {msg4!r}", not ok4)


@test
def test_add_rejects_a_non_directory(ctx: Ctx):
    from halo_harness.providers.local_model_dirs import add_model_dir
    with _Env():
        ok, msg = add_model_dir(str(Path(tempfile.mkdtemp()) / "not-there"))
        ctx.check(f"a missing path is refused, got {msg!r}", not ok)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

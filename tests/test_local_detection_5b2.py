"""tests.test_local_detection_5b2 -- Halo 2.0.3 round 5b part 2 (brief
items 5/6): mlx_lm.server/llama-server disambiguation, LM Studio's model
folder joining the hub-cache scan (fixture tree), mlx-community hub-cache
labelling, and the wizard's `what_fits_at_32k`/`detection_summary_lines`.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()
_GB = 1024 ** 3


# ---- MLX vs llama-server disambiguation ----------------------------------

@test
def test_runtime_label_llama_server_when_props_answers(ctx: Ctx):
    from halo_harness.providers.huggingface_local_probe import _runtime_label_for
    label = _runtime_label_for("http://127.0.0.1:8080/v1", props_ctx=4096)
    ctx.check(f"llama-server, got {label!r}", label == "llama-server")


@test
def test_runtime_label_mlx_on_darwin_without_props(ctx: Ctx):
    from halo_harness.providers import huggingface_local_probe as mod
    import sys as _sys
    old = _sys.platform
    try:
        _sys.platform = "darwin"
        label = mod._runtime_label_for("http://127.0.0.1:8080/v1", props_ctx=None)
        ctx.check(f"mlx (heuristic: no /props, darwin), got {label!r}", label == "mlx")
    finally:
        _sys.platform = old


@test
def test_runtime_label_unknown_on_other_platforms_without_props(ctx: Ctx):
    from halo_harness.providers import huggingface_local_probe as mod
    import sys as _sys
    old = _sys.platform
    try:
        _sys.platform = "linux"
        label = mod._runtime_label_for("http://127.0.0.1:8080/v1", props_ctx=None)
        ctx.check(f"unknown on Linux (never guess MLX off Apple Silicon), got {label!r}", label is None)
    finally:
        _sys.platform = old


@test
def test_detected_local_server_runtime_label_wired_through(ctx: Ctx):
    """End to end (real platform, never monkeypatched -- forcing
    `sys.platform` around a REAL network call confuses unrelated stdlib
    code, e.g. `urllib.request`'s own platform-specific proxy detection,
    which is exactly what `test_runtime_label_mlx_on_darwin_without_props`
    above avoids by testing `_runtime_label_for` in isolation instead):
    a fake server with no model-reported context and no `/props` gets
    `runtime_label=None` on THIS (non-Darwin) box, wired through onto the
    `DetectedLocalServer` row exactly as `probe_models_endpoint` builds it."""
    from tests.helpers.mock_openai import MockUpstream
    from halo_harness.providers.huggingface_local_probe import probe_models_endpoint
    import sys as _sys
    mock = MockUpstream(path_prefix="/v1").start()
    try:
        mock.models_response = {"data": [{"id": "some-model"}]}  # no context field, no /props -> 404s
        info = probe_models_endpoint(mock.base_url, name="auto:test")
        ctx.check("server detected", info is not None)
        expected = "mlx" if _sys.platform == "darwin" else None
        ctx.check(f"runtime_label matches this platform's own rule, got {info.runtime_label!r}",
                  info.runtime_label == expected)
    finally:
        mock.stop()


@test
def test_local_models_group_label_shows_mlx(ctx: Ctx):
    from halo_harness.providers.huggingface_local_probe import DetectedLocalServer
    from halo_harness.providers.local_models import _hf_server_rows
    info = DetectedLocalServer(name="auto:8080", base_url="http://127.0.0.1:8080/v1",
                                model_ids=("m1",), runtime_label="mlx")
    rows = _hf_server_rows(info, reachable=True, addressable=True)
    ctx.check(f"group label mentions MLX, got {rows[0].group!r}", "MLX" in rows[0].group)


# ---- LM Studio folder scan ------------------------------------------------

@test
def test_scan_lmstudio_models_finds_gguf_and_safetensors(ctx: Ctx):
    from halo_harness.providers.lmstudio_cache import scan_lmstudio_models
    root = Path(tempfile.mkdtemp(prefix="lmstudio-fixture-"))
    try:
        gguf_dir = root / "publisher-a" / "model-one"
        gguf_dir.mkdir(parents=True)
        (gguf_dir / "model-one.Q4_0.gguf").write_bytes(b"x" * 1024)

        st_dir = root / "publisher-b" / "model-two"
        st_dir.mkdir(parents=True)
        (st_dir / "config.json").write_text("{}", encoding="utf-8")
        (st_dir / "model.safetensors").write_bytes(b"y" * 2048)

        rows = scan_lmstudio_models(root=root)
        ctx.check(f"two models found, got {len(rows)}", len(rows) == 2)
        names = {r.repo_id for r in rows}
        ctx.check(f"gguf file found by relative path, got {names!r}",
                  any("model-one.Q4_0.gguf" in n for n in names))
        ctx.check(f"safetensors folder found by relative path, got {names!r}",
                  any(n == "publisher-b/model-two" for n in names))
        formats = {r.formats for r in rows}
        ctx.check(f"formats tagged correctly, got {formats!r}", ("gguf",) in formats and ("safetensors",) in formats)
    finally:
        pass


@test
def test_scan_lmstudio_models_missing_dir_returns_empty(ctx: Ctx):
    from halo_harness.providers.lmstudio_cache import scan_lmstudio_models
    rows = scan_lmstudio_models(root=Path(tempfile.mkdtemp(prefix="lmstudio-empty-")) / "does-not-exist")
    ctx.check("empty, never an error", rows == [])


@test
def test_resolve_lmstudio_models_dir_default_and_override(ctx: Ctx):
    import os
    from halo_harness.providers.lmstudio_cache import resolve_lmstudio_models_dir
    from halo_harness.theme import set_config_value
    d = Path(tempfile.mkdtemp(prefix="lmstudio-cfg-"))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    try:
        default = resolve_lmstudio_models_dir()
        ctx.check(f"default is ~/.lmstudio/models, got {default}", default.parts[-2:] == (".lmstudio", "models"))
        set_config_value("huggingface.lmstudio_models_dir", str(d / "custom"))
        overridden = resolve_lmstudio_models_dir()
        ctx.check(f"config override honoured, got {overridden}", overridden == d / "custom")
    finally:
        os.environ.pop("BRIDGE_STATE_DIR", None)


# ---- mlx-community hub-cache labelling + build_local_view wiring --------

@test
def test_mlx_community_hub_repo_labelled_runnable_through_mlx(ctx: Ctx):
    import os
    from halo_harness.providers.local_models import build_local_view
    root = Path(tempfile.mkdtemp(prefix="hubcache-mlx-"))
    state_dir = Path(tempfile.mkdtemp(prefix="hubcache-mlx-state-"))
    old_state_dir = os.environ.get("BRIDGE_STATE_DIR")
    old_no_net = os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET")
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
    os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"
    try:
        d = root / "models--mlx-community--Some-Model-4bit"
        (d / "blobs").mkdir(parents=True)
        (d / "snapshots" / "main").mkdir(parents=True)
        (d / "blobs" / "abc").write_bytes(b"z" * 4096)
        try:
            (d / "snapshots" / "main" / "model.safetensors").symlink_to(d / "blobs" / "abc")
        except OSError:
            pass  # symlinks may need privilege on this Windows box -- formats detection isn't this test's point
        rows = build_local_view(env={"HF_HUB_CACHE": str(root)})
        mlx_rows = [r for r in rows if r.group == "Hugging Face (cache, not served)" and "mlx-community" in r.name]
        ctx.check(f"the mlx-community repo is present, got {[r.name for r in rows]!r}", len(mlx_rows) == 1)
        ctx.check(f"labelled runnable through MLX, got {mlx_rows[0].capability!r}",
                  "runnable through MLX" in mlx_rows[0].capability)
    finally:
        if old_state_dir is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old_state_dir
        if old_no_net is None:
            os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        else:
            os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = old_no_net


# ---- what_fits_at_32k / detection_summary_lines --------------------------

@test
def test_what_fits_at_32k_none_for_no_free_memory(ctx: Ctx):
    from halo_harness.providers.local_models import what_fits_at_32k
    ctx.check("None input -> []", what_fits_at_32k(None) == [])
    ctx.check("non-positive -> []", what_fits_at_32k(0) == [])
    ctx.check("tiny amount (less than the reserve) -> []", what_fits_at_32k(1 * _GB) == [])


@test
def test_what_fits_at_32k_picks_largest_classes_first(ctx: Ctx):
    from halo_harness.providers.local_models import what_fits_at_32k
    # A huge card -- the 70B class at q4_0 should be the first result.
    fits = what_fits_at_32k(200 * _GB)
    ctx.check(f"at least one class fits, got {fits!r}", len(fits) >= 1)
    ctx.check(f"the largest class (70B) is offered first, got {fits!r}", fits[0].startswith("70B"))
    ctx.check("never more than two classes", len(fits) <= 2)


@test
def test_detection_summary_lines_never_raises_with_everything_unreachable(ctx: Ctx):
    """Under BRIDGE_TEST_NO_BACKGROUND_NET, every live probe this
    function calls is a no-op -- it must still return a (possibly empty
    or GPU-only) list of plain strings, never raise. `state_dir` passed
    explicitly -- never the real `~/.halo` (house rule)."""
    import os
    old_no_net = os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET")
    os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"
    try:
        from halo_harness.providers.local_models import detection_summary_lines
        state_dir = Path(tempfile.mkdtemp(prefix="detect-summary-state-"))
        lines = detection_summary_lines(state_dir=state_dir)
        ctx.check("a list of strings, never raises", isinstance(lines, list) and all(isinstance(l, str) for l in lines))
    finally:
        if old_no_net is None:
            os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        else:
            os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = old_no_net


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

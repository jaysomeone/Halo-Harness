"""tests.test_huggingface_mlx_extras -- Halo 2.0.3 round 5f: the
pyproject.toml `mlx` extra's own marker text, the `/local` hub-cache
hint (`hf:mlx/<repo>` ref + the explicit `--runtime mlx_lm` form), and
`halo local serve <repo> --runtime mlx_lm`'s bare-repo-id fallback
(`providers.local_use.serve_local_model`) against a fake runtime stub.
Never touches the real ~/.halo.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()

_STUB_LISTENS = '''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[sys.argv.index("--port") + 1])


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({"object": "list", "data": [{"id": "stub-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


@test
def test_pyproject_mlx_extra_marker(ctx: Ctx):
    text = (REPO_DIR / "pyproject.toml").read_text(encoding="utf-8")
    ctx.check("an [project.optional-dependencies] mlx group exists", "mlx = [" in text)
    ctx.check("it depends on mlx-lm", "mlx-lm" in text)
    ctx.check("the environment marker restricts to darwin", "sys_platform == 'darwin'" in text)
    ctx.check("the environment marker restricts to arm64", "platform_machine == 'arm64'" in text)


_LOCAL_VIEW_ENV_NAMES = ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "OLLAMA_HOST", "OLLAMA_API_KEY",
                         "HF_HUB_CACHE", "HF_HOME", "HF_LOCAL_PROBE_PORTS", "BRIDGE_TEST_NO_BACKGROUND_NET")


@test
def test_local_view_hub_cache_mlx_row_shows_hf_mlx_ref_and_explicit_hint(ctx: Ctx):
    """Same env-scoping discipline as `tests/test_local_models.py`'s own
    `_Env` (its module docstring: an unscoped `build_local_view` call
    found a REAL Ollama catalog and a REAL Hugging Face Hub cache entry
    on the build host while writing that file) -- `OLLAMA_HOST` pinned
    unreachable, `HF_HUB_CACHE` pinned to this test's own fixture tree."""
    import halo_harness.providers.local_runtime as local_runtime_mod
    from halo_harness.providers.local_models import build_local_view
    saved = {k: os.environ.get(k) for k in _LOCAL_VIEW_ENV_NAMES}
    # `runtime_for_format("mlx")` only returns "mlx_lm" when `sys.platform
    # == "darwin"` -- this suite never runs on a real Mac, and faking
    # `sys.platform` itself is unsafe here (urllib's own proxy detection
    # also reads it, and tries to import a macOS-only `_scproxy` module
    # that genuinely doesn't exist on this host -- `build_local_view`
    # touches that code indirectly via its Ollama-reachability probe).
    # `runtime_for_format` itself is monkeypatched instead -- the ONE
    # function this test actually needs a different answer from.
    orig_runtime_for_format = local_runtime_mod.runtime_for_format
    local_runtime_mod.runtime_for_format = lambda fmt: ("mlx_lm" if fmt in ("safetensors", "mlx") else
                                                         orig_runtime_for_format(fmt))
    try:
        d = Path(tempfile.mkdtemp(prefix="hub-cache-mlx-"))
        root = d / "hub-cache"
        repo_dir = root / "models--mlx-community--Test-4bit"
        snap = repo_dir / "snapshots" / "rev1"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}", encoding="utf-8")
        (snap / "model.safetensors").write_bytes(b"\x00" * 16)
        (repo_dir / "blobs").mkdir(parents=True)

        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["OLLAMA_HOST"] = "http://127.0.0.1:1"  # unreachable
        os.environ.pop("OLLAMA_API_KEY", None)
        os.environ["HF_HUB_CACHE"] = str(root)
        os.environ.pop("HF_HOME", None)
        os.environ.pop("HF_LOCAL_PROBE_PORTS", None)
        os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"

        rows = build_local_view(env=dict(os.environ), state_dir=d / ".halo")
        mlx_rows = [r for r in rows if r.group == "Hugging Face (cache, not served)"
                    and "mlx-community/Test-4bit" in r.name]
        ctx.check(f"exactly one row for the mlx repo, got {rows!r}", len(mlx_rows) == 1)
        row = mlx_rows[0]
        ctx.check(f"ref is the hf:mlx form, got {row.ref!r}", row.ref == "hf:mlx/mlx-community/Test-4bit")
        ctx.check(f"capability names the explicit --runtime mlx_lm form, got {row.capability!r}",
                  "--runtime mlx_lm" in row.capability)
    finally:
        local_runtime_mod.runtime_for_format = orig_runtime_for_format
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _write_stub() -> Path:
    d = Path(tempfile.mkdtemp(prefix="local-use-mlx-stub-"))
    p = d / "stub_mlx.py"
    p.write_text(_STUB_LISTENS, encoding="utf-8")
    return p


@test
def test_serve_local_model_bare_repo_id_with_runtime_mlx_lm_delegates_to_ensure(ctx: Ctx):
    """`halo local serve <mlx repo or folder> --runtime mlx_lm` is the
    brief's own documented "explicit form" -- a bare Hub repo id (never
    resolvable as a file) with `--runtime mlx_lm` must delegate to the
    SAME `hf:mlx/<repo>` machinery `--model hf:mlx/<repo>` uses, keyed by
    the repo id itself."""
    import halo_harness.providers.huggingface_mlx as hf_mlx
    import halo_harness.providers.local_runtime as local_runtime_mod
    from halo_harness.providers.local_runtime import load_registry, stop_managed_server
    from halo_harness.providers.local_use import serve_local_model

    saved = {k: os.environ.get(k) for k in ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR")}
    orig_is_apple = hf_mlx.is_apple_silicon
    orig_find_binary = local_runtime_mod.find_runtime_binary
    hf_mlx.is_apple_silicon = lambda **_kw: True  # force the platform gate open for this test
    home = Path(tempfile.mkdtemp(prefix="local-use-mlx-home-"))
    state_dir = home / ".halo"
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
    stub = _write_stub()
    # `ensure_mlx_server` (called by `serve_local_model`'s own fallback,
    # which exposes no `binary_argv` test seam of its own) imports
    # `find_runtime_binary` fresh off this module at call time -- the real
    # one would look for a real `mlx-lm` pip install, which this hermetic
    # suite never has. Monkeypatched to return the fake stub's argv
    # instead, restored in `finally`.
    local_runtime_mod.find_runtime_binary = lambda *_a, **_kw: [sys.executable, str(stub)]
    repo = "mlx-community/serve-fallback-test"
    try:
        ok, lines = serve_local_model(repo, runtime="mlx_lm", confirm=lambda _q: True, state_dir=state_dir,
                                       env={})
        ctx.check(f"serve succeeded, got lines={lines!r}", ok is True)
        recorded = load_registry(state_dir)
        ctx.check(f"registry keyed by the repo id, got {recorded}",
                  len(recorded) == 1 and recorded[0].get("model") == repo)
    finally:
        hf_mlx.is_apple_silicon = orig_is_apple
        local_runtime_mod.find_runtime_binary = orig_find_binary
        stop_managed_server(repo, state_dir=state_dir)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

"""tests.test_local_use -- Halo 2.0.3 round 5c: the end-to-end `halo local
serve`/`import`/`runtime remove` orchestration (providers.local_use),
against fixture model files, a fake runtime stub, a fake GitHub release,
and an injected `confirm` callable -- never real stdin, never a real
download, never a real Ollama daemon.
"""
from __future__ import annotations

import json
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.mock_ollama import MockUpstream
from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_STUB_LISTENS = '''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[sys.argv.index("--port") + 1])


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "stub-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def _write_gguf_fixture() -> Path:
    def enc_str(s):
        b = s.encode("utf-8")
        return struct.pack("<Q", len(b)) + b

    def enc_kv(key, vtype, value):
        out = enc_str(key) + struct.pack("<I", vtype)
        return out + (struct.pack("<I", value) if vtype == 4 else enc_str(value))

    kvs = [enc_kv("general.architecture", 8, "qwen3"), enc_kv("qwen3.context_length", 4, 8192),
           enc_kv("qwen3.block_count", 4, 4), enc_kv("qwen3.attention.head_count", 4, 4),
           enc_kv("qwen3.attention.head_count_kv", 4, 2), enc_kv("qwen3.embedding_length", 4, 256)]
    data = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(kvs)) + b"".join(kvs)
    d = Path(tempfile.mkdtemp(prefix="local-use-model-"))
    p = d / "tiny-test-model.gguf"
    p.write_bytes(data)
    return p


def _write_stub() -> Path:
    d = Path(tempfile.mkdtemp(prefix="local-use-stub-"))
    p = d / "stub.py"
    p.write_text(_STUB_LISTENS, encoding="utf-8")
    return p


@test
def test_resolve_local_file_by_exact_path_and_by_name(ctx: Ctx):
    from halo_harness.providers.local_use import resolve_local_file
    p = _write_gguf_fixture()
    direct = resolve_local_file(str(p))
    ctx.check(f"exact path resolves, got {direct}", direct is not None and direct.path == p)
    by_name = resolve_local_file(p.stem, env={})
    ctx.check(f"bare name finds nothing without model_dirs configured, got {by_name}", by_name is None)


@test
def test_serve_local_model_unknown_model_fails_plainly(ctx: Ctx):
    from halo_harness.providers.local_use import serve_local_model
    ok, lines = serve_local_model("no-such-model-anywhere", state_dir=Path(tempfile.mkdtemp()))
    ctx.check(f"fails plainly, got ({ok}, {lines})", ok is False and "no file-backed model" in lines[0])


@test
def test_serve_local_model_with_runtime_already_on_a_fake_path(ctx: Ctx):
    """Forces the "binary already found" branch by pointing PATH-less
    resolution straight at a working stub via --runtime override is not
    possible (find_runtime_binary only searches real PATH/runtimes_dir),
    so this test installs the stub INTO the runtimes_dir fixture layout
    find_runtime_binary already searches (pinned by test_local_runtime.py)
    -- the simplest way to reach start_managed_server without a real
    llama-server and without exercising the fetch-consent path at all."""
    from halo_harness.providers.local_runtime import runtimes_dir
    from halo_harness.providers.local_use import serve_local_model
    state_dir = Path(tempfile.mkdtemp(prefix="local-use-serve-"))
    model_path = _write_gguf_fixture()
    exe_name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    exe_dir = runtimes_dir(state_dir) / "b1" / "bin"
    exe_dir.mkdir(parents=True)
    # The "binary" is a tiny wrapper script with the SAME name find_runtime_
    # binary searches for, re-exec'd through python so it actually runs the
    # real listening stub regardless of the exe's own (fake) bytes on disk.
    import shutil
    stub = _write_stub()
    if sys.platform == "win32":
        (exe_dir / exe_name).write_text(f'@"{sys.executable}" "{stub}" %*\n', encoding="utf-8")
        real_binary = exe_dir / exe_name
    else:
        (exe_dir / exe_name).write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n', encoding="utf-8")
        (exe_dir / exe_name).chmod(0o755)
        real_binary = exe_dir / exe_name
    del shutil
    ok, lines = serve_local_model(str(model_path), state_dir=state_dir, confirm=lambda _q: False)
    if sys.platform == "win32":
        # A .bat-style wrapper written with a plain .write_text (no .bat
        # extension) is not directly executable by CreateProcess on this
        # OS -- documented limitation of this one fixture technique, not a
        # production code path (production always finds a REAL llama-
        # server.exe, which Windows Popen runs directly); skip the
        # assertions that need it to actually have started below.
        ctx.check("ran without raising on Windows regardless of the wrapper's own executability", True)
        return
    ctx.check(f"serve succeeds via the discovered 'binary', got {lines}", ok)
    from halo_harness.providers.local_runtime import load_registry, stop_all_managed_servers_except_kept
    try:
        recorded = load_registry(state_dir)
        ctx.check(f"registered with the gguf stem as model id, got {recorded}",
                  any(e.get("model") == "tiny-test-model" for e in recorded))
    finally:
        stop_all_managed_servers_except_kept(state_dir=state_dir)


@test
def test_serve_local_model_declines_the_fetch_when_confirm_says_no(ctx: Ctx):
    from halo_harness.providers.local_use import serve_local_model
    state_dir = Path(tempfile.mkdtemp(prefix="local-use-decline-"))
    model_path = _write_gguf_fixture()
    # A win32/vulkan asset -- matches THIS test host's real platform
    # whatever it is; `hw_runner` forces "no CUDA version detected" so the
    # backend choice (and therefore which asset must be present in the
    # fixture) is deterministic regardless of this build host's real GPU
    # (round 5c found this host DOES have a real NVIDIA card -- without
    # this override, probe_nvidia_cuda_version's real nvidia-smi call
    # would pick "cuda" here and this fixture would need a cuda asset
    # instead).
    asset_name = {"win32": "llama-b1-bin-win-vulkan-x64.zip", "darwin": "llama-b1-bin-macos-arm64.tar.gz"}.get(
        sys.platform, "llama-b1-bin-ubuntu-vulkan-x64.tar.gz")
    fake_release = {"tag_name": "b1", "prerelease": True, "assets": [
        {"name": asset_name, "browser_download_url": "https://x/a", "digest": "sha256:aaa", "size": 10}]}
    ok, lines = serve_local_model(str(model_path), state_dir=state_dir, confirm=lambda _q: False,
                                   releases=[fake_release], hw_runner=lambda argv, timeout: None)
    ctx.check(f"declined cleanly, got ({ok}, {lines})",
              ok is False and any("Declined" in ln for ln in lines))


@test
def test_import_local_model_end_to_end_against_mock_ollama(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost, resolve_ollama_hosts
    from halo_harness.providers.local_use import import_local_model
    model_path = _write_gguf_fixture()
    with MockUpstream() as mock:
        import halo_harness.providers.ollama as ollama_mod
        real_resolve = ollama_mod.resolve_ollama_host
        ollama_mod.resolve_ollama_host = lambda name=None, env=None: OllamaHost(name="default", url=mock.base_url)
        try:
            ok, lines = import_local_model(str(model_path), name="my-import", confirm=lambda _q: True)
        finally:
            ollama_mod.resolve_ollama_host = real_resolve
        ctx.check(f"import succeeds, got {lines}", ok)
        create_calls = [r for r in mock.requests if r["path"].rstrip("/") == "/api/create"]
        ctx.check(f"exactly one /api/create call with the right name, got {create_calls}",
                  len(create_calls) == 1 and create_calls[0]["body"]["model"] == "my-import")


@test
def test_import_local_model_declines_without_confirm(ctx: Ctx):
    from halo_harness.providers.local_use import import_local_model
    model_path = _write_gguf_fixture()
    ok, lines = import_local_model(str(model_path), confirm=lambda _q: False)
    ctx.check(f"declined, got ({ok}, {lines})", ok is False and "Declined" in lines[-1])


# ---- ensure_llama_server_binary: --backend override, cuda_runtime_installed skip ----

_FAKE_CUDA_RELEASE = {"tag_name": "b11398", "prerelease": True, "assets": [
    {"name": "llama-b11398-bin-win-cuda-12.4-x64.zip", "browser_download_url": "https://x/main",
     "digest": "sha256:aaa", "size": 100},
    {"name": "cudart-llama-b11398-bin-win-cuda-12.4-x64.zip", "browser_download_url": "https://x/rt",
     "digest": "sha256:bbb", "size": 50},
    {"name": "llama-b11398-bin-win-vulkan-x64.zip", "browser_download_url": "https://x/vk",
     "digest": "sha256:ccc", "size": 10},
    {"name": "llama-b11398-bin-ubuntu-cuda-12.4-x64.tar.gz", "browser_download_url": "https://x/main-nix",
     "digest": "sha256:ddd", "size": 100},
    {"name": "cudart-llama-b11398-bin-ubuntu-cuda-12.4-x64.tar.gz", "browser_download_url": "https://x/rt-nix",
     "digest": "sha256:eee", "size": 50},
    {"name": "llama-b11398-bin-ubuntu-vulkan-x64.tar.gz", "browser_download_url": "https://x/vk-nix",
     "digest": "sha256:fff", "size": 10},
]}


def _asset_echo_lines(lines: list) -> list:
    """The `"  <asset name>: <url>"` echo lines only -- EXCLUDES the
    first (consent-sentence) line, whose embedded `dest_dir` PATH could
    itself happen to contain a substring a test is searching for (a real
    bug this file's own earlier draft hit: a tempdir named
    `ensure-skip-cudart-<random>` made a bare `"cudart" in line` check on
    line 0 always true, regardless of which asset was actually chosen),
    and the LAST (decline/result) line."""
    return lines[1:-1]


@test
def test_ensure_llama_server_binary_backend_override_bypasses_cuda_probe(ctx: Ctx):
    from halo_harness.providers.local_use import ensure_llama_server_binary
    state_dir = Path(tempfile.mkdtemp(prefix="ensure-override-"))
    probed = []

    def hw_runner(argv, timeout):
        probed.append(argv)
        return "CUDA Version                            : 12.4"  # a real driver -- override must still win

    binary, lines = ensure_llama_server_binary(state_dir=state_dir, confirm=lambda _q: False,
                                                releases=[_FAKE_CUDA_RELEASE], hw_runner=hw_runner,
                                                backend_override="vulkan", platform_name="win32")
    echo = _asset_echo_lines(lines)
    ctx.check(f"declined (never fetched for real), got {lines}", binary is None)
    ctx.check(f"the vulkan asset was the one offered, got {echo}", any("vulkan" in ln for ln in echo))
    ctx.check(f"cudart was never offered for a vulkan override, got {echo}",
              not any("cudart" in ln for ln in echo))


@test
def test_ensure_llama_server_binary_skips_cudart_when_toolkit_already_installed(ctx: Ctx):
    """FIX PASS (Kali live run): `platform_name="win32"` is passed
    EXPLICITLY -- the asset NAME (and therefore its extension, `.zip` on
    Windows vs `.tar.gz` on Linux) must be pinned to the injected
    platform, never to whichever real OS happens to run this suite (the
    code was already picking the right asset for ITS host; the
    assertion was the platform-shaped part, matching only a `.zip` name)."""
    from halo_harness.providers.local_use import ensure_llama_server_binary
    state_dir = Path(tempfile.mkdtemp(prefix="ensure-toolkit-present-"))
    binary, lines = ensure_llama_server_binary(state_dir=state_dir, confirm=lambda _q: False,
                                                releases=[_FAKE_CUDA_RELEASE],
                                                hw_runner=lambda argv, timeout: "CUDA Version                            : 12.4",
                                                cuda_runtime_installed=True, platform_name="win32")
    echo = _asset_echo_lines(lines)
    ctx.check(f"declined (never fetched for real), got {lines}", binary is None)
    ctx.check(f"exactly one asset offered (the main cuda build), got {echo}", len(echo) == 1)
    ctx.check(f"it is the main cuda build, never the cudart redistributable, got {echo}",
              "cuda-12.4-x64.zip" in echo[0] and "cudart" not in echo[0])


@test
def test_ensure_llama_server_binary_skips_cudart_when_toolkit_already_installed_linux(ctx: Ctx):
    """The SAME scenario, pinned to the other OS's asset shape (`.tar.gz`,
    `-ubuntu-` in the name) -- added alongside the Windows-pinned test
    above rather than replacing it, so both OS's asset-name shapes stay
    covered regardless of which one happens to run this suite."""
    from halo_harness.providers.local_use import ensure_llama_server_binary
    state_dir = Path(tempfile.mkdtemp(prefix="ensure-toolkit-present-linux-"))
    binary, lines = ensure_llama_server_binary(state_dir=state_dir, confirm=lambda _q: False,
                                                releases=[_FAKE_CUDA_RELEASE],
                                                hw_runner=lambda argv, timeout: "CUDA Version                            : 12.4",
                                                cuda_runtime_installed=True, platform_name="linux")
    echo = _asset_echo_lines(lines)
    ctx.check(f"declined (never fetched for real), got {lines}", binary is None)
    ctx.check(f"exactly one asset offered (the main cuda build), got {echo}", len(echo) == 1)
    ctx.check(f"it is the main cuda build, never the cudart redistributable, got {echo}",
              "cuda-12.4-x64.tar.gz" in echo[0] and "cudart" not in echo[0])


@test
def test_ensure_llama_server_binary_includes_cudart_when_toolkit_absent(ctx: Ctx):
    from halo_harness.providers.local_use import ensure_llama_server_binary
    state_dir = Path(tempfile.mkdtemp(prefix="ensure-toolkit-absent-"))
    binary, lines = ensure_llama_server_binary(state_dir=state_dir, confirm=lambda _q: False,
                                                releases=[_FAKE_CUDA_RELEASE],
                                                hw_runner=lambda argv, timeout: "CUDA Version                            : 12.4",
                                                cuda_runtime_installed=False, platform_name="win32")
    echo = _asset_echo_lines(lines)
    ctx.check(f"declined (never fetched for real), got {lines}", binary is None)
    ctx.check(f"BOTH assets offered this time (main + cudart), got {echo}", len(echo) == 2)
    ctx.check(f"the cudart companion is one of them, got {echo}", any("cudart" in ln for ln in echo))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

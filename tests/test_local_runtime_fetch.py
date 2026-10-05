"""tests.test_local_runtime_fetch -- Halo 2.0.3 round 5c, FIX PASS after
the live run: the llama.cpp release-LIST walk (never `/releases/latest`,
which points at a non-binary release live), the corrected CUDA
major/minor selection rule with 12.x fallback, the paired `cudart-*`
redistributable, `has_cuda_runtime_installed`, and digest verification --
against a fixture copied from the REAL releases JSON shape (tags,
prerelease flags, asset names, sizes, digests). Never the real network.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_MB = 1024 * 1024


def _asset(name: str, size_mb: float) -> dict:
    return {"name": name, "browser_download_url": f"https://github.com/ggml-org/llama.cpp/releases/download/x/{name}",
            "digest": f"sha256:{hashlib.sha256(name.encode()).hexdigest()}", "size": int(size_mb * _MB)}


# Live-run fixture (facts from the fix-pass brief): /releases/latest would
# point here -- NOT a b-tagged binary release, must never be selected.
_NON_BINARY_RELEASE = {"tag_name": "v0.5.0", "prerelease": False,
                       "assets": [_asset("llama.cpp-v0.5.0-source.tar.gz", 2.0)]}

# The newest real binary release seen live: b11398, 36 assets (subset
# relevant to these tests reproduced here).
_B11398 = {"tag_name": "b11398", "prerelease": True, "assets": [
    _asset("llama-b11398-bin-win-cuda-12.4-x64.zip", 251.6),
    _asset("cudart-llama-b11398-bin-win-cuda-12.4-x64.zip", 373.3),
    _asset("llama-b11398-bin-win-cuda-13.4-x64.zip", 145.9),
    _asset("cudart-llama-b11398-bin-win-cuda-13.4-x64.zip", 403.9),
    _asset("llama-b11398-bin-win-vulkan-x64.zip", 31.8),
    _asset("llama-b11398-bin-win-cpu-x64.zip", 20.0),
    _asset("llama-b11398-bin-macos-arm64.tar.gz", 30.0),
    _asset("llama-b11398-bin-macos-x64.tar.gz", 28.0),
    _asset("llama-b11398-bin-ubuntu-x64.tar.gz", 22.0),
    _asset("llama-b11398-bin-ubuntu-cuda-12.8-x64.tar.gz", 240.0),
    _asset("cudart-llama-b11398-bin-ubuntu-cuda-12.8-x64.tar.gz", 360.0),
    _asset("llama-b11398-bin-ubuntu-cuda-13.4-x64.tar.gz", 150.0),
    _asset("cudart-llama-b11398-bin-ubuntu-cuda-13.4-x64.tar.gz", 400.0),
    _asset("llama-b11398-bin-ubuntu-vulkan-x64.tar.gz", 33.0),
    _asset("llama-b11398-bin-ubuntu-rocm-10.0-x64.tar.gz", 200.0),
]}

# A NEWER tag that only shipped a cpu asset (a real occurrence: a given
# tag is not guaranteed to carry every asset) -- pins "search backward
# through the list, never just the newest tag".
_B11399_PARTIAL = {"tag_name": "b11399", "prerelease": True,
                   "assets": [_asset("llama-b11399-bin-win-cpu-x64.zip", 20.1)]}

_RELEASE_LIST = [_B11399_PARTIAL, _NON_BINARY_RELEASE, _B11398]


@test
def test_backend_candidates_chain(ctx: Ctx):
    from halo_harness.providers.local_runtime_fetch import backend_candidates
    ctx.check("darwin is metal-only, no fallback",
              backend_candidates(platform_name="darwin", cuda_version="12.8") == ["metal"])
    ctx.check("a driver CUDA version tries cuda then vulkan",
              backend_candidates(platform_name="win32", cuda_version="13.2") == ["cuda", "vulkan"])
    ctx.check("no driver -> vulkan alone",
              backend_candidates(platform_name="win32", cuda_version=None) == ["vulkan"])
    ctx.check("--backend overrides outright, no chain",
              backend_candidates(platform_name="win32", cuda_version="13.2", override="cpu") == ["cpu"])


@test
def test_select_release_and_assets_win32_driver_13_2_falls_back_to_cuda_12_4_pair(ctx: Ctx):
    from halo_harness.providers.local_runtime_fetch import select_release_and_assets
    release, chosen = select_release_and_assets(_RELEASE_LIST, platform_name="win32", backend="cuda",
                                                 cuda_version="13.2")
    ctx.check(f"the binary release, not v0.5.0, got {release['tag_name']}", release["tag_name"] == "b11398")
    names = sorted(a["name"] for a in chosen)
    ctx.check(f"12.4 main+cudart pair (13.4's minor 4 > driver minor 2), got {names}",
              names == ["cudart-llama-b11398-bin-win-cuda-12.4-x64.zip", "llama-b11398-bin-win-cuda-12.4-x64.zip"])


@test
def test_select_release_and_assets_win32_driver_13_4_picks_cuda_13_4_pair(ctx: Ctx):
    from halo_harness.providers.local_runtime_fetch import select_release_and_assets
    _release, chosen = select_release_and_assets(_RELEASE_LIST, platform_name="win32", backend="cuda",
                                                  cuda_version="13.4")
    names = sorted(a["name"] for a in chosen)
    ctx.check(f"13.4 main+cudart pair (minor 4 <= driver minor 4), got {names}",
              names == ["cudart-llama-b11398-bin-win-cuda-13.4-x64.zip", "llama-b11398-bin-win-cuda-13.4-x64.zip"])


@test
def test_select_release_and_assets_linux_driver_12_8_picks_cuda_12_8_pair(ctx: Ctx):
    from halo_harness.providers.local_runtime_fetch import select_release_and_assets
    _release, chosen = select_release_and_assets(_RELEASE_LIST, platform_name="linux", backend="cuda",
                                                  cuda_version="12.8")
    names = sorted(a["name"] for a in chosen)
    ctx.check(f"ubuntu cuda-12.8 pair, got {names}",
              names == ["cudart-llama-b11398-bin-ubuntu-cuda-12.8-x64.tar.gz",
                        "llama-b11398-bin-ubuntu-cuda-12.8-x64.tar.gz"])


@test
def test_select_release_and_assets_darwin_metal(ctx: Ctx):
    from halo_harness.providers.local_runtime_fetch import select_release_and_assets
    _release, chosen = select_release_and_assets(_RELEASE_LIST, platform_name="darwin", backend="metal")
    ctx.check(f"macos-arm64, single asset, got {[a['name'] for a in chosen]}",
              [a["name"] for a in chosen] == ["llama-b11398-bin-macos-arm64.tar.gz"])


@test
def test_select_release_and_assets_no_nvidia_means_vulkan(ctx: Ctx):
    from halo_harness.providers.local_runtime_fetch import select_release_and_assets
    _release, chosen = select_release_and_assets(_RELEASE_LIST, platform_name="win32", backend="vulkan",
                                                  cuda_version=None)
    ctx.check(f"win vulkan asset, got {[a['name'] for a in chosen]}",
              [a["name"] for a in chosen] == ["llama-b11398-bin-win-vulkan-x64.zip"])


@test
def test_select_release_and_assets_backend_override_vulkan(ctx: Ctx):
    """--backend vulkan: bypasses the cuda-version algorithm outright,
    even with a real driver version present."""
    from halo_harness.providers.local_runtime_fetch import select_release_and_assets
    _release, chosen = select_release_and_assets(_RELEASE_LIST, platform_name="win32", backend="vulkan",
                                                  cuda_version="13.2")
    ctx.check(f"vulkan asset regardless of the driver version, got {[a['name'] for a in chosen]}",
              [a["name"] for a in chosen] == ["llama-b11398-bin-win-vulkan-x64.zip"])


@test
def test_select_release_and_assets_searches_backward_past_a_partial_newer_tag(ctx: Ctx):
    """b11399 (newest) ships only a cpu asset -- asking for vulkan must
    search backward to b11398 rather than failing outright."""
    from halo_harness.providers.local_runtime_fetch import select_release_and_assets
    release, chosen = select_release_and_assets(_RELEASE_LIST, platform_name="win32", backend="vulkan")
    ctx.check(f"found on the OLDER b11398, not the newer partial b11399, got {release['tag_name']}",
              release["tag_name"] == "b11398")
    ctx.check(f"the vulkan asset itself, got {[a['name'] for a in chosen]}",
              [a["name"] for a in chosen] == ["llama-b11398-bin-win-vulkan-x64.zip"])
    # cpu, however, IS on the newest tag -- confirms "newest that carries
    # the asset", not "always the second-newest".
    release_cpu, _chosen_cpu = select_release_and_assets(_RELEASE_LIST, platform_name="win32", backend="cpu")
    ctx.check(f"cpu found on the newest tag b11399, got {release_cpu['tag_name']}",
              release_cpu["tag_name"] == "b11399")


@test
def test_select_release_and_assets_none_when_nothing_matches(ctx: Ctx):
    from halo_harness.providers.local_runtime_fetch import select_release_and_assets
    ctx.check("no rocm backend recognized at all -> None",
              select_release_and_assets(_RELEASE_LIST, platform_name="win32", backend="rocm") is None)


@test
def test_has_cuda_runtime_installed_via_injected_path_dirs(ctx: Ctx):
    from halo_harness.providers.local_runtime_fetch import has_cuda_runtime_installed
    empty_dir = Path(tempfile.mkdtemp(prefix="no-cuda-"))
    ctx.check("nothing on PATH -> False (win32)",
              has_cuda_runtime_installed(platform_name="win32", path_dirs=[str(empty_dir)]) is False)
    ctx.check("nothing on PATH -> False (linux)",
              has_cuda_runtime_installed(platform_name="linux", path_dirs=[str(empty_dir)]) is False)
    win_dir = Path(tempfile.mkdtemp(prefix="has-cuda-win-"))
    (win_dir / "cudart64_120.dll").write_bytes(b"")
    ctx.check("cudart64_*.dll present -> True (win32)",
              has_cuda_runtime_installed(platform_name="win32", path_dirs=[str(win_dir)]) is True)
    linux_dir = Path(tempfile.mkdtemp(prefix="has-cuda-linux-"))
    (linux_dir / "libcudart.so.12").write_bytes(b"")
    ctx.check("libcudart.so* present -> True (linux)",
              has_cuda_runtime_installed(platform_name="linux", path_dirs=[str(linux_dir)]) is True)
    ctx.check("macOS has no CUDA -- always True (never blocks)",
              has_cuda_runtime_installed(platform_name="darwin", path_dirs=[str(empty_dir)]) is True)


@test
def test_verify_digest_matching_and_mismatching(ctx: Ctx):
    from halo_harness.providers.local_runtime_fetch import verify_digest
    d = Path(tempfile.mkdtemp(prefix="digest-"))
    p = d / "asset.bin"
    data = b"pretend llama-server binary bytes"
    p.write_bytes(data)
    real_hex = hashlib.sha256(data).hexdigest()
    ctx.check("matching digest -> True", verify_digest(p, f"sha256:{real_hex}") is True)
    ctx.check("mismatching digest -> False", verify_digest(p, "sha256:" + "0" * 64) is False)
    ctx.check("missing digest -> False", verify_digest(p, None) is False)


@test
def test_consent_sentence_names_size_destination_and_vulkan_alternative(ctx: Ctx):
    from halo_harness.providers.local_runtime_fetch import consent_sentence
    text = consent_sentence(["llama-b11398-bin-win-cuda-12.4-x64.zip", "cudart-llama-b11398-bin-win-cuda-12.4-x64.zip"],
                             (251.6 + 373.3) * _MB, "/home/u/.halo/runtimes/b11398",
                             vulkan_alternative_size=int(31.8 * _MB))
    ctx.check(f"names both assets, got {text!r}", "llama-b11398-bin-win-cuda-12.4-x64.zip" in text
              and "cudart-llama-b11398-bin-win-cuda-12.4-x64.zip" in text)
    ctx.check(f"names the destination, got {text!r}", "/home/u/.halo/runtimes/b11398" in text)
    from halo_harness.providers.ollama_panel import human_bytes
    ctx.check(f"mentions --backend vulkan as the smaller alternative with its size, got {text!r}",
              "--backend vulkan" in text and human_bytes(int(31.8 * _MB)) in text)


@test
def test_fetch_release_list_parses_a_fake_list_response(ctx: Ctx):
    import json as _json
    from halo_harness.providers.local_runtime_fetch import fetch_release_list

    def fetcher(url, headers):
        ctx.check(f"lists /releases, not /releases/latest, got {url}", "/releases?" in url and "latest" not in url)
        return 200, _json.dumps(_RELEASE_LIST).encode("utf-8")
    got = fetch_release_list(fetcher=fetcher)
    ctx.check(f"parsed as a list, got {type(got)}", isinstance(got, list) and len(got) == 3)


@test
def test_fetch_and_install_end_to_end_against_a_fake_release(ctx: Ctx):
    from halo_harness.providers.local_runtime import find_runtime_binary
    from halo_harness.providers.local_runtime_fetch import fetch_and_install_llama_server
    state_dir = Path(tempfile.mkdtemp(prefix="runtime-fetch-state-"))
    work_dir = Path(tempfile.mkdtemp(prefix="runtime-fetch-work-"))
    exe_name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    zip_path = Path(tempfile.mktemp(suffix=".zip"))
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(exe_name, b"pretend binary")
    asset_bytes = zip_path.read_bytes()
    asset_name = "llama-b11398-bin-win-cpu-x64.zip"
    assets = [{"name": asset_name, "browser_download_url": f"https://x/{asset_name}",
               "digest": f"sha256:{hashlib.sha256(asset_bytes).hexdigest()}", "size": len(asset_bytes)}]

    def fetcher(url, headers):
        return 200, asset_bytes
    ok, msg = fetch_and_install_llama_server(state_dir=state_dir, version_tag="b11398", assets=assets,
                                              fetcher=fetcher, tmp_dir=work_dir)
    ctx.check(f"install reports success, got {msg!r}", ok)
    found = find_runtime_binary("llama-server", state_dir=state_dir)
    ctx.check(f"the fetched binary is now findable, got {found}", found is not None)


@test
def test_fetch_and_install_fails_on_digest_mismatch(ctx: Ctx):
    from halo_harness.providers.local_runtime_fetch import fetch_and_install_llama_server
    state_dir = Path(tempfile.mkdtemp(prefix="runtime-fetch-bad-"))
    asset_name = "llama-b11398-bin-ubuntu-x64.tar.gz"
    assets = [{"name": asset_name, "browser_download_url": f"https://x/{asset_name}",
               "digest": "sha256:" + "0" * 64, "size": 5}]

    def fetcher(url, headers):
        return 200, b"wrong bytes"
    ok, msg = fetch_and_install_llama_server(state_dir=state_dir, version_tag="b11398", assets=assets,
                                              fetcher=fetcher)
    ctx.check(f"digest mismatch is refused, got ({ok}, {msg!r})", ok is False and "digest" in msg.lower())


@test
def test_remove_runtime(ctx: Ctx):
    from halo_harness.providers.local_runtime import runtimes_dir
    from halo_harness.providers.local_runtime_fetch import remove_runtime
    state_dir = Path(tempfile.mkdtemp(prefix="runtime-remove-"))
    target = runtimes_dir(state_dir) / "b11398"
    target.mkdir(parents=True)
    (target / "marker").write_text("x", encoding="utf-8")
    ok, msg = remove_runtime(state_dir, "b11398")
    ctx.check(f"remove succeeds, got {msg!r}", ok and not target.exists())
    ok2, msg2 = remove_runtime(state_dir, "b11398")
    ctx.check(f"removing again fails plainly, got {msg2!r}", not ok2)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))

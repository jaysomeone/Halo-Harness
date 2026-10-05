"""halo_harness.providers.local_runtime_fetch -- Halo 2.0.3 round 5c (brief
item 3), FIX PASS after the live run: fetches a pinned llama.cpp release
asset for this OS/GPU backend into `~/.halo/runtimes/<tag>/`, verified
against the GitHub Releases API's own per-asset `digest` field (confirmed
live: `"sha256:<hex>"`). Never on PATH; `halo local runtime remove`
deletes it.

Live-run correction: `GET /releases/latest` points at the project's most
recent NON-binary release (tag `v0.5.0`, one asset) -- the actual compiled
builds are PRERELEASES tagged `b<number>` (confirmed live: newest seen
`b11398`, 36 assets). `fetch_release_list` lists `GET /releases?per_page=N`
instead; `select_release_and_assets` walks that list NEWEST-tag-first and
picks the first release that actually carries the asset(s) this OS/backend
needs (a given tag is not guaranteed to ship every asset).

Live-run correction: the CUDA pick is no longer "highest asset version <=
driver version" outright -- confirmed live (driver reporting CUDA 13.2,
assets only at 12.4/13.4): same-MAJOR builds whose minor is <= the driver's
minor win first (13.2 -> no 13.x qualifies, since 13.4's minor 4 > 2); when
none qualify, fall back to the highest available 12.x build (never a
different major); only when neither exists does the caller fall back to
Vulkan. A CUDA pick also drags in the paired `cudart-*` redistributable
UNLESS a CUDA toolkit is already on this machine (`has_cuda_runtime_
installed`: a `cudart64_*.dll` on PATH on Windows, `libcudart.so*` on
Linux).

The real network calls go through `urllib.request` (auto-follows the
redirect a GitHub release asset's `browser_download_url` issues), injected
via `fetcher` for tests -- NOT `providers/http.py`'s `open_upstream` (that
module is today a raw socket+TLS opener with no redirect-following; round
5e's own planned "one HTTP choke point for offline mode" is the natural
place to fold this in later, once it exists).
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger("bridge")

# Asset NAME patterns per (platform, backend), `sys.platform` values as the
# platform key -- `llama-<tag>-bin-<os>-<backend>[-<ver>]-<arch>.<ext>`.
# "cuda_runtime" is the paired `cudart-*` redistributable asset (a `%s`
# template filled with the chosen CUDA version string once known).
ASSET_PATTERNS = {
    ("linux", "cpu"): r"^llama-.+-bin-ubuntu-x64\.tar\.gz$",
    ("linux", "vulkan"): r"^llama-.+-bin-ubuntu-vulkan-x64\.tar\.gz$",
    ("linux", "cuda"): r"^llama-.+-bin-ubuntu-cuda-(?P<ver>[\d.]+)-x64\.tar\.gz$",
    ("linux", "cuda_runtime"): r"^cudart-llama-.+-bin-ubuntu-cuda-%s-x64\.tar\.gz$",
    ("win32", "cpu"): r"^llama-.+-bin-win-cpu-x64\.zip$",
    ("win32", "vulkan"): r"^llama-.+-bin-win-vulkan-x64\.zip$",
    ("win32", "cuda"): r"^llama-.+-bin-win-cuda-(?P<ver>[\d.]+)-x64\.zip$",
    ("win32", "cuda_runtime"): r"^cudart-llama-.+-bin-win-cuda-%s-x64\.zip$",
    ("darwin", "metal"): r"^llama-.+-bin-macos-arm64\.tar\.gz$",
    ("darwin", "cpu"): r"^llama-.+-bin-macos-x64\.tar\.gz$",
}

_BUILD_TAG_RE = re.compile(r"^b(\d+)$")


def backend_candidates(*, platform_name: str, cuda_version: Optional[str], override: Optional[str] = None) -> list:
    """Priority-ordered backends to try, best first. `override`
    (`--backend`) short-circuits to exactly that one choice -- explicit
    always wins, no fallback chain. Otherwise: Metal alone on macOS (no
    fallback -- Apple Silicon's default-on build); CUDA then Vulkan when
    the driver reported a CUDA version (the live-run's own "else Vulkan"
    correction); Vulkan alone otherwise."""
    if override:
        return [override]
    if platform_name == "darwin":
        return ["metal"]
    if cuda_version:
        return ["cuda", "vulkan"]
    return ["vulkan"]


def _parse_version(v: str) -> tuple:
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return (0,)


def _select_cuda_candidate(assets: list, platform_name: str, cuda_version: Optional[str]):
    """`(ver_str, asset)` for the winning CUDA build, or `None`. Rule
    (live-run correction, see module docstring): same major as the
    driver with minor <= the driver's minor wins (highest such minor);
    else the highest available 12.x build; else `None` (no 12.x either
    -- the caller falls back to Vulkan)."""
    pattern = ASSET_PATTERNS.get((platform_name, "cuda"))
    if not pattern or not cuda_version:
        return None
    driver = _parse_version(cuda_version)
    driver_major = driver[0] if driver else None
    driver_minor = driver[1] if len(driver) > 1 else 0
    candidates = []
    for a in assets:
        m = re.match(pattern, a.get("name") or "")
        if not m:
            continue
        candidates.append((_parse_version(m.group("ver")), m.group("ver"), a))
    if not candidates:
        return None
    same_major = [c for c in candidates
                  if c[0][0] == driver_major and (c[0][1] if len(c[0]) > 1 else 0) <= driver_minor]
    if same_major:
        same_major.sort(key=lambda c: c[0])
        return same_major[-1][1], same_major[-1][2]
    twelve_x = [c for c in candidates if c[0][0] == 12]
    if twelve_x:
        twelve_x.sort(key=lambda c: c[0])
        return twelve_x[-1][1], twelve_x[-1][2]
    return None


def pick_assets(assets: "list[dict]", *, platform_name: str, backend: str,
                 cuda_version: Optional[str] = None) -> "Optional[list[dict]]":
    """One or two asset dicts (main + the paired `cudart-*` for CUDA) from
    ONE release's `assets` list -- `None` when nothing matches this
    (platform, backend) at all."""
    if backend == "cuda":
        candidate = _select_cuda_candidate(assets, platform_name, cuda_version)
        if candidate is None:
            return None
        ver_str, main = candidate
        runtime_tpl = ASSET_PATTERNS.get((platform_name, "cuda_runtime"))
        runtime = None
        if runtime_tpl:
            runtime_pattern = runtime_tpl % re.escape(ver_str)
            runtime = next((a for a in assets if re.match(runtime_pattern, a.get("name") or "")), None)
        return [main, runtime] if runtime else [main]
    pattern = ASSET_PATTERNS.get((platform_name, backend))
    if not pattern:
        return None
    match = next((a for a in assets if re.match(pattern, a.get("name") or "")), None)
    return [match] if match else None


def _build_tag_releases_newest_first(releases: "list[dict]") -> "list[dict]":
    """Every release whose tag matches `^b\\d+$` (the live-run's own
    finding: THIS pattern, not the `prerelease` boolean, is what actually
    distinguishes a compiled-binary release here), newest numeric tag
    first."""
    tagged = []
    for r in releases or []:
        if not isinstance(r, dict):
            continue
        m = _BUILD_TAG_RE.match(r.get("tag_name") or "")
        if m:
            tagged.append((int(m.group(1)), r))
    tagged.sort(key=lambda t: t[0], reverse=True)
    return [r for _n, r in tagged]


def select_release_and_assets(releases: "list[dict]", *, platform_name: str, backend: str,
                               cuda_version: Optional[str] = None):
    """`(release, chosen_assets)` for the NEWEST `b<number>`-tagged
    release that actually carries the needed asset(s) -- `None` when no
    release in the list does. A given tag is not guaranteed to ship every
    asset, so this searches backward through the list rather than only
    ever looking at the newest one."""
    for release in _build_tag_releases_newest_first(releases):
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        chosen = pick_assets(assets, platform_name=platform_name, backend=backend, cuda_version=cuda_version)
        if chosen:
            return release, chosen
    return None


def has_cuda_runtime_installed(*, platform_name: Optional[str] = None, path_dirs: "Optional[list]" = None) -> bool:
    """Whether a CUDA toolkit/runtime is already on this machine (a
    `cudart64_*.dll` on `PATH` on Windows, `libcudart.so*` on Linux) --
    when True, the paired `cudart-*` redistributable asset is skipped.
    macOS has no CUDA at all, so this is always True (irrelevant) there.
    `path_dirs` replaces a `$PATH` split (the test seam); never raises."""
    platform_name = platform_name or sys.platform
    if platform_name not in ("win32", "linux"):
        return True
    if path_dirs is None:
        path_dirs = (os.environ.get("PATH") or "").split(os.pathsep)
    pattern = "cudart64_*.dll" if platform_name == "win32" else "libcudart.so*"
    for d in path_dirs:
        try:
            if glob.glob(os.path.join(d, pattern)):
                return True
        except OSError:
            continue
    return False


def verify_digest(file_path, digest: "Optional[str]") -> bool:
    """GitHub's per-asset `digest` field -- confirmed live, `"sha256:
    <hex>"`. `False` (never raises) on a missing/empty digest, an
    unrecognized algorithm name, or an actual mismatch."""
    if not digest:
        return False
    algo, _, hexval = digest.partition(":")
    if not hexval:
        algo, hexval = "sha256", digest
    try:
        hasher = hashlib.new(algo.lower().replace("-", "_"))
    except ValueError:
        return False
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                hasher.update(chunk)
    except OSError:
        return False
    return hasher.hexdigest().lower() == hexval.lower()


def _default_fetcher(url: str, headers: dict) -> "tuple[int, bytes]":
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=20) as resp:  # auto-follows the asset redirect
        return resp.status, resp.read()


def fetch_release_list(repo: str = "ggml-org/llama.cpp", *, per_page: int = 10, fetcher=None) -> Optional[list]:
    """`GET /repos/<repo>/releases?per_page=<n>` -- `None` on any failure
    (never raises). `fetcher(url, headers) -> (status, body_bytes)` is
    the full network seam; tests always inject their own, built from a
    fixture copied from the real releases JSON shape."""
    fetcher = fetcher or _default_fetcher
    try:
        status, body = fetcher(f"https://api.github.com/repos/{repo}/releases?per_page={per_page}",
                                {"Accept": "application/vnd.github+json"})
    except Exception as e:
        log.debug("local_runtime_fetch: fetch_release_list failed: %s", e)
        return None
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def download_asset(url: str, dest_path, *, fetcher=None) -> "tuple[bool, str]":
    fetcher = fetcher or _default_fetcher
    try:
        status, body = fetcher(url, {})
    except Exception as e:
        return False, f"download failed: {e}"
    if status != 200:
        return False, f"download failed: HTTP {status}"
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(body)
    return True, f"downloaded {len(body)} bytes"


def unpack_archive(archive_path, dest_dir) -> "tuple[bool, str]":
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive_path = str(archive_path)
    try:
        if archive_path.endswith(".zip"):
            import zipfile
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(dest_dir)
        else:
            import tarfile
            with tarfile.open(archive_path) as tf:
                try:
                    tf.extractall(dest_dir, filter="data")  # py3.12+: refuse path traversal/odd members
                except TypeError:
                    tf.extractall(dest_dir)  # older Python without the `filter` kwarg
    except Exception as e:
        return False, f"unpack failed: {e}"
    return True, f"unpacked into {dest_dir}"


def consent_sentence(asset_names: "list[str]", total_size_bytes: int, dest_dir, *,
                      vulkan_alternative_size: Optional[int] = None) -> str:
    """Brief item 3: "names the size, the URL and the destination" --
    URLs are shown separately by the caller right before this sentence.
    Fix pass: also names the smaller `--backend vulkan` alternative's
    size when one was found, for a CUDA pick (two downloads, often
    hundreds of MB)."""
    from halo_harness.providers.ollama_panel import human_bytes
    names = " and ".join(asset_names)
    text = (f"Halo found no llama-server runtime on PATH or in {dest_dir} and can download {names} "
            f"({human_bytes(total_size_bytes)} total) and unpack it there -- never added to PATH, "
            f"removable later with `halo local runtime remove`.")
    if vulkan_alternative_size is not None:
        text += (f" A smaller, slower `--backend vulkan` build ({human_bytes(vulkan_alternative_size)}) "
                  f"is also available instead.")
    return text


def fetch_and_install_llama_server(*, state_dir, version_tag: str, assets: "list[dict]", fetcher=None,
                                    tmp_dir: Optional[Path] = None) -> "tuple[bool, str]":
    """Downloads, verifies (every asset's own `digest`), and unpacks the
    ALREADY-CHOSEN `assets` (see `select_release_and_assets`/
    `has_cuda_runtime_installed` -- this function re-selects nothing) into
    `~/.halo/runtimes/<version_tag>/`. Call only AFTER the caller already
    printed `consent_sentence` and got a yes. `(False, reason)` on the
    first failure; nothing already unpacked from an earlier asset is
    rolled back, matching "never silent" with a plain reason rather than
    a half-silent partial install."""
    from halo_harness.providers.local_runtime import runtimes_dir
    if not assets:
        return False, "no assets chosen to install"
    dest_dir = runtimes_dir(state_dir) / version_tag
    work_dir = Path(tmp_dir) if tmp_dir is not None else Path(tempfile.mkdtemp(prefix="halo-runtime-dl-"))
    for asset in assets:
        archive_path = work_dir / asset["name"]
        ok, msg = download_asset(asset["browser_download_url"], archive_path, fetcher=fetcher)
        if not ok:
            return False, msg
        if not verify_digest(archive_path, asset.get("digest")):
            return False, f"digest verification failed for {asset['name']}"
        ok2, msg2 = unpack_archive(archive_path, dest_dir)
        if not ok2:
            return False, msg2
    return True, f"installed into {dest_dir}"


def remove_runtime(state_dir, version_tag: Optional[str] = None) -> "tuple[bool, str]":
    """`halo local runtime remove [VERSION]` -- removes one installed
    version, or every installed version when `version_tag` is omitted."""
    import shutil
    from halo_harness.providers.local_runtime import runtimes_dir
    root = runtimes_dir(state_dir)
    target = (root / version_tag) if version_tag else root
    if not target.is_dir():
        return False, f"nothing installed at {target}"
    shutil.rmtree(target, ignore_errors=True)
    return True, f"removed {target}"
